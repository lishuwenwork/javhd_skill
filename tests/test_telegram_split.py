"""电报专用分段：真实本地媒体、顺序发布、清理及夸克原文件隔离；不连接线上服务。"""

import asyncio
from contextlib import asynccontextmanager, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import imageio_ffmpeg
from telethon import functions, types

from common import JobError
import common
import main
import quark
import source
import telegram
import telegram_media
from d1_testing import install_capture


def packet_checksums(path):
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-copyts", "-start_at_zero", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "framecrc", "-",
    ], capture_output=True, text=True, check=True)
    packets = {}
    for line in result.stdout.splitlines():
        if line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        stream = int(fields[0])
        packets.setdefault(stream, []).append((int(fields[4]), fields[5]))
    return packets


class SplitDirectionConfigTests(unittest.TestCase):
    def test_legacy_config_defaults_to_tail_without_rewriting_it(self):
        template = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        template.pop("telegram_split_direction", None)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(template), encoding="utf-8")
            before = path.read_bytes()
            with patch.object(common, "ROOT", Path(temp)), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(common.load_config()["telegram_split_direction"], "tail")
            self.assertEqual(path.read_bytes(), before)

    def test_only_head_and_tail_config_values_are_accepted(self):
        template = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        for value in ("head", "tail", "HEAD", "end", True, None, [], {}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "config.json").write_text(json.dumps({**template, "telegram_split_direction": value}), encoding="utf-8")
                with patch.object(common, "ROOT", Path(temp)), patch.dict(os.environ, {}, clear=True):
                    if value in ("head", "tail"):
                        self.assertEqual(common.load_config()["telegram_split_direction"], value)
                    else:
                        with self.assertRaisesRegex(JobError, "telegram_split_direction"):
                            common.load_config()


class SplitDirectionPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_tail_uses_packet_bytes_and_returns_forward_ordered_cut_points(self):
        class Output:
            def __init__(self, timestamps):
                self.lines = iter([b"#tb 0: 1/1\n", *[
                    f"0, {value}, {value}, 1, 96, 0x12345678\n".encode() for value in timestamps]])
            def __aiter__(self):
                return self
            async def __anext__(self):
                try:
                    return next(self.lines)
                except StopIteration:
                    raise StopAsyncIteration
        for times in (list(range(11)), [0, 5, 6, 10, 20, 21, 25, 30, 31, 50, 80]):
            with self.subTest(timestamps=times):
                def process(*args, **kwargs):
                    return SimpleNamespace(stdout=Output(times), returncode=0, wait=AsyncMock(return_value=0))
                with patch.object(telegram_media.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=process)):
                    head = await telegram_media.split_points(Path("test.mp4"), 512, direction="head")
                    tail = await telegram_media.split_points(Path("test.mp4"), 512, direction="tail")
                    default = await telegram_media.split_points(Path("test.mp4"), 512)
                self.assertEqual(head, [float(times[4]), float(times[8])])
                self.assertEqual(tail, [float(times[3]), float(times[7])])
                self.assertEqual(tail, sorted(tail))
                self.assertEqual(default, tail)

    async def test_invalid_direction_does_not_launch_ffmpeg(self):
        with patch.object(telegram_media.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as start:
            with self.assertRaises(JobError):
                await telegram_media.split_points(Path("test.mp4"), 512, direction="sideways")
        start.assert_not_awaited()


class SplitMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_and_audio_are_preserved_in_bounded_playable_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "测试中文视频.mp4"
            await source.ffmpeg(
                "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12",
                "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=24000",
                "-t", "8", "-c:v", "libx264", "-g", "6", "-keyint_min", "6", "-sc_threshold", "0",
                "-c:a", "aac", "-movflags", "+faststart", video,
            )
            original = video.read_bytes()
            media = SimpleNamespace(video=video, video_meta=source.media_metadata(video))
            before = await asyncio.to_thread(packet_checksums, video)
            for direction in ("head", "tail"):
                with self.subTest(direction=direction), redirect_stdout(StringIO()):
                    after = {stream: [] for stream in before}
                    async with telegram_media.telegram_videos(media, max_bytes=60000, direction=direction) as parts:
                        self.assertGreaterEqual(len(parts), 3)
                        paths = [path for path, _ in parts]
                        self.assertEqual([path.name for path in paths],
                                         [f"测试中文视频({index}).mp4" for index in range(1, len(paths) + 1)])
                        for path, metadata in parts:
                            self.assertLessEqual(path.stat().st_size, 60000)
                            self.assertGreater(path.stat().st_size, 0)
                            self.assertEqual((metadata["width"], metadata["height"]), (160, 120))
                            self.assertGreater(metadata["duration"], 0)
                            await source.ffmpeg("-xerror", "-err_detect", "explode", "-i", path,
                                                "-map", "0:v:0", "-f", "null", "-")
                            for stream, values in (await asyncio.to_thread(packet_checksums, path)).items():
                                after[stream].extend(values)
                        self.assertEqual(video.read_bytes(), original)
                    self.assertEqual(after, before)
                    self.assertTrue(all(not path.exists() for path in paths))
                    self.assertEqual(list(root.glob("telegram-parts-*")), [])
                    self.assertEqual(video.read_bytes(), original)

    async def test_at_or_below_limit_reuses_original_without_ffmpeg(self):
        for size in (99, 100):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temp:
                video = Path(temp) / "原视频.mp4"
                video.write_bytes(b"x" * size)
                media = SimpleNamespace(video=video, video_meta={"duration": 1})
                with patch.object(telegram_media, "split_points", new_callable=AsyncMock) as plan, \
                        patch.object(telegram_media, "ffmpeg", new_callable=AsyncMock) as convert:
                    async with telegram_media.telegram_videos(media, max_bytes=100) as parts:
                        self.assertEqual(parts, [(video, media.video_meta)])
                plan.assert_not_awaited()
                convert.assert_not_awaited()
                self.assertTrue(video.exists())

    async def test_failed_split_removes_partial_outputs_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "原视频.mp4"
            video.write_bytes(b"original" * 1000)
            original = video.read_bytes()
            media = SimpleNamespace(video=video, video_meta={"duration": 20})
            async def fail(*args):
                Path(str(args[-1]).replace("%06d", "000001")).write_bytes(b"partial")
                raise JobError("模拟切割失败")
            with patch.object(telegram_media, "split_points", new=AsyncMock(return_value=[1.0])), \
                    patch.object(telegram_media, "ffmpeg", side_effect=fail):
                with self.assertRaises(JobError):
                    async with telegram_media.telegram_videos(media, max_bytes=5000):
                        self.fail("失败分段不能进入上传")
            self.assertEqual(video.read_bytes(), original)
            self.assertEqual(list(root.glob("telegram-parts-*")), [])

    async def test_size_is_checked_after_remux_and_never_yields_oversized_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "原视频.mp4"
            video.write_bytes(b"original" * 1000)
            media = SimpleNamespace(video=video, video_meta={"duration": 20})
            async def oversize(*args):
                Path(str(args[-1]).replace("%06d", "000001")).write_bytes(b"x" * 5001)
                Path(str(args[-1]).replace("%06d", "000002")).write_bytes(b"x" * 50)
            with patch.object(telegram_media, "split_points", new=AsyncMock(return_value=[1.0])), \
                    patch.object(telegram_media, "ffmpeg", side_effect=oversize):
                with self.assertRaises(JobError):
                    async with telegram_media.telegram_videos(media, max_bytes=5000):
                        self.fail("不得把超限文件交给电报")
            self.assertEqual(list(root.glob("telegram-parts-*")), [])

    async def test_oversized_first_attempt_is_replanned_before_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "原视频.mp4"
            video.write_bytes(b"original" * 1000)
            media = SimpleNamespace(video=video, video_meta={"duration": 20})
            attempts = 0
            async def remux(*args):
                nonlocal attempts
                attempts += 1
                pattern = str(args[-1])
                Path(pattern.replace("%06d", "000001")).write_bytes(b"x" * (5001 if attempts == 1 else 4000))
                Path(pattern.replace("%06d", "000002")).write_bytes(b"x" * 500)
            plan = AsyncMock(return_value=[1.0])
            with patch.object(telegram_media, "split_points", new=plan), \
                    patch.object(telegram_media, "ffmpeg", side_effect=remux), \
                    patch.object(telegram_media, "media_metadata", return_value={"duration": 10}), \
                    redirect_stdout(StringIO()):
                async with telegram_media.telegram_videos(media, max_bytes=5000) as parts:
                    self.assertEqual(len(parts), 2)
                    self.assertTrue(all(path.stat().st_size <= 5000 for path, _ in parts))
            self.assertEqual(attempts, 2)
            self.assertLess(plan.call_args_list[1].args[1], plan.call_args_list[0].args[1])
            self.assertEqual(list(root.glob("telegram-parts-*")), [])

    async def test_cancelled_packet_scan_terminates_ffmpeg(self):
        entered = asyncio.Event()
        class Output:
            def __aiter__(self):
                return self
            async def __anext__(self):
                entered.set()
                await asyncio.Event().wait()
        process = SimpleNamespace(stdout=Output(), returncode=None, kill=MagicMock(),
                                  wait=AsyncMock(return_value=-9))
        with patch.object(telegram_media.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)):
            task = asyncio.create_task(telegram_media.split_points(Path("test.mp4"), 5000))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited()


class SplitUploadTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, count, *, fail_batch=0, direction="head"):
        now = datetime.now(timezone.utc)
        albums, documents = [], []
        async def call(request):
            if isinstance(request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)):
                return True
            if isinstance(request, functions.messages.UploadMediaRequest):
                if isinstance(request.media, types.InputMediaUploadedPhoto):
                    return SimpleNamespace(photo=types.Photo(1, 2, b"ref", now, [], 2))
                documents.append(request.media)
                return SimpleNamespace(document=types.Document(len(documents) + 20, 2, b"ref", now,
                                                               "video/mp4", 10, 2, []))
            if isinstance(request, functions.messages.SendMultiMediaRequest):
                albums.append(request)
                if len(albums) == fail_batch:
                    return SimpleNamespace(updates=[])
                ids = list(range(100 * len(albums), 100 * len(albums) + len(request.multi_media)))
                return SimpleNamespace(updates=[
                    *[types.UpdateMessageID(id=n, random_id=item.random_id)
                      for n, item in zip(ids, request.multi_media)],
                    *[types.UpdateNewChannelMessage(types.Message(
                        id=n, peer_id=types.PeerChannel(1), grouped_id=len(albums)), 1, 1) for n in ids],
                ])
            raise AssertionError(type(request))
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"upload_concurrency": 2, "telegram_split_direction": direction}
        uploader.peer = AsyncMock(return_value=types.InputPeerChannel(1, 2))
        uploader.call = telegram.FloodGate(call)
        uploader.client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {name: root / f"{name}.mp4" for name in ("video", "cover", "thumb", "preview")}
            for path in files.values():
                path.write_bytes(b"original")
            metadata = {"width": 640, "height": 360, "duration": 5}
            media = SimpleNamespace(**files, info=SimpleNamespace(title="示例中文标题"),
                                    video_meta=metadata, preview_meta=metadata)
            expected_direction = direction
            @asynccontextmanager
            async def split(_media, *, premium=False, direction="head", retain=False):
                self.assertIs(_media, media)
                self.assertFalse(premium)
                self.assertFalse(retain)
                self.assertEqual(direction, expected_direction)
                paths = [root / f"示例中文标题({n}).mp4" for n in range(1, count + 1)]
                for path in paths:
                    path.write_bytes(b"part")
                try:
                    yield [(path, metadata) for path in paths]
                finally:
                    for path in paths:
                        path.unlink()
            with patch.object(telegram, "telegram_videos", split), redirect_stdout(StringIO()):
                if fail_batch:
                    with self.assertRaisesRegex(JobError, "请先检查频道"):
                        await uploader.upload(media, ["#ID123", "#4k"])
                    ids = []
                else:
                    ids = await uploader.upload(media, ["#ID123", "#4k"])
            if fail_batch:
                self.assertEqual(uploader.video_message_ids, list(range(101, 100 + len(albums[0].multi_media))))
            else:
                self.assertEqual(uploader.video_message_ids, ids[1:])
            self.assertEqual(files["video"].read_bytes(), b"original")
            self.assertEqual(list(root.glob("示例中文标题(*).mp4")), [])
        return ids, albums, documents

    async def test_numbered_parts_keep_cover_tags_and_original_order(self):
        ids, albums, documents = await self.exercise(3)
        self.assertEqual(len(ids), 4)
        self.assertEqual([len(album.multi_media) for album in albums], [4])
        filenames = [next(a.file_name for a in doc.attributes if isinstance(a, types.DocumentAttributeFilename))
                     for doc in documents]
        self.assertEqual(filenames, ["preview.mp4", "示例中文标题(1).mp4", "示例中文标题(2).mp4", "示例中文标题(3).mp4"])
        for index, item in enumerate(albums[0].multi_media[1:], 1):
            self.assertEqual(item.message, f"示例中文标题({index}/3)\n\n#ID123 #4k")
            self.assertEqual(len(item.entities), 2)
            self.assertIsNotNone(documents[index].video_cover)
            self.assertIsNotNone(documents[index].thumb)
        self.assertEqual(sum(bool(doc.nosound_video) for doc in documents), 1)

    async def test_many_parts_form_ordered_albums_with_one_preview(self):
        for count, expected in ((10, [9, 2]), (12, [10, 3]), (20, [10, 9, 2])):
            with self.subTest(count=count):
                ids, albums, documents = await self.exercise(count)
                self.assertEqual(len(ids), count + 1)
                self.assertEqual([len(album.multi_media) for album in albums], expected)
                self.assertEqual(sum(bool(doc.nosound_video) for doc in documents), 1)

    async def test_tail_selection_keeps_first_file_as_part_one(self):
        ids, albums, documents = await self.exercise(3, direction="tail")
        filenames = [next(a.file_name for a in doc.attributes if isinstance(a, types.DocumentAttributeFilename))
                     for doc in documents]
        self.assertEqual(filenames, ["preview.mp4", "示例中文标题(1).mp4", "示例中文标题(2).mp4", "示例中文标题(3).mp4"])
        self.assertEqual(len(ids), 4)
        self.assertTrue(albums[0].multi_media[1].message.startswith("示例中文标题(1/3)"))

    async def test_later_album_without_confirmation_is_not_overall_success(self):
        _, albums, _ = await self.exercise(12, fail_batch=2)
        self.assertEqual(len(albums), 2)


class TargetIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def test_quark_receives_original_while_telegram_prepares_parts(self):
        loop = asyncio.get_running_loop()
        quark_started = asyncio.Event()
        telegram_failed = threading.Event()
        seen = []
        original = b"complete original" * 1000
        async def prepare(_, directory, _config, **kwargs):
            path = directory / "完整原视频.mp4"
            path.write_bytes(original)
            return SimpleNamespace(video=path, video_meta={"duration": 20})
        async def fail_split(path, _budget, *, direction="head"):
            self.assertEqual(path.read_bytes(), original)
            await asyncio.wait_for(quark_started.wait(), 2)
            telegram_failed.set()
            raise JobError("模拟电报切割失败")
        async def telegram_upload(media, tags):
            async with telegram_media.telegram_videos(media, max_bytes=100) as parts:
                self.fail("切割失败不应进入电报发布")
        def quark_upload(media, **_kwargs):
            seen.append(media.video.read_bytes())
            loop.call_soon_threadsafe(quark_started.set)
            self.assertTrue(telegram_failed.wait(2))
            seen.append(media.video.read_bytes())
            return "quark-original"
        tg = SimpleNamespace(login=AsyncMock(), upload=telegram_upload, notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=quark_upload, close=MagicMock())
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "ROOT", Path(temp)), \
                patch.object(main, "STATE", Path(temp) / ".state"), \
                patch.object(source, "prepare_media", side_effect=prepare), \
                patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                patch.object(telegram_media, "split_points", side_effect=fail_split), \
                redirect_stdout(StringIO()):
            self.assertEqual(await main.operate({"notify_saved_messages": False}, video_id="123"), 1)
            result = self.capture.result("123")
            self.assertEqual(result["telegram"], "failed")
            self.assertEqual(result["quark"], "success")
            self.assertFalse(result["cleaned"])
            self.assertTrue(result["retained"])
        self.assertEqual(seen, [original, original])


if __name__ == "__main__":
    unittest.main()
