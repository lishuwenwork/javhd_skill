"""会员免切割、普通账号兼容和任一平台成功后的持久 ID 记录；不连接线上服务。"""

import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from telethon import functions, types

import common
import main
import quark
import source
import telegram
import telegram_media
from d1_testing import install_capture
from task_files import TaskFiles


class MembershipConfigTests(unittest.TestCase):
    def test_legacy_config_defaults_to_non_premium_without_changing_file(self):
        template = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        template.pop("telegram_premium", None)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(template), encoding="utf-8")
            original = path.read_bytes()
            with patch.object(common, "ROOT", Path(temp)), patch.dict(os.environ, {}, clear=True):
                self.assertIs(common.load_config()["telegram_premium"], False)
            self.assertEqual(path.read_bytes(), original)

    def test_premium_requires_json_boolean(self):
        template = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        for value in (True, False, "true", "false", 0, 1, None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "config.json").write_text(json.dumps({**template, "telegram_premium": value}), encoding="utf-8")
                with patch.object(common, "ROOT", Path(temp)), patch.dict(os.environ, {}, clear=True):
                    if type(value) is bool:
                        self.assertIs(common.load_config()["telegram_premium"], value)
                    else:
                        with self.assertRaisesRegex(common.JobError, "telegram_premium"):
                            common.load_config()


class PremiumUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_premium_reuses_original_above_ordinary_limit_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "完整视频.mp4"
            video.write_bytes(b"x" * 150)
            media = SimpleNamespace(video=video, video_meta={"duration": 20})
            with patch.object(telegram_media, "split_points", new_callable=AsyncMock) as split, \
                    patch.object(telegram_media, "ffmpeg", new_callable=AsyncMock) as convert:
                async with telegram_media.telegram_videos(media, max_bytes=100, premium=True, direction="tail") as parts:
                    self.assertEqual(parts, [(video, media.video_meta)])
            split.assert_not_awaited()
            convert.assert_not_awaited()
            self.assertEqual(video.stat().st_size, 150)

    async def test_above_premium_limit_is_rejected_without_cutting(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "完整视频.mp4"
            video.write_bytes(b"x" * 201)
            media = SimpleNamespace(video=video, video_meta={"duration": 20})
            with patch.object(telegram_media, "PREMIUM_MAX_VIDEO_SIZE", 200), \
                    patch.object(telegram_media, "split_points", new_callable=AsyncMock) as split:
                with self.assertRaisesRegex(common.JobError, "会员"):
                    async with telegram_media.telegram_videos(media, max_bytes=100, premium=True):
                        self.fail("超出会员上限不能进入上传，也不能自动切割")
            split.assert_not_awaited()

    async def test_premium_limit_is_passed_to_chunk_uploader(self):
        now = datetime.now(timezone.utc)
        documents = []
        async def call(request):
            if isinstance(request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)):
                return True
            if isinstance(request, functions.messages.UploadMediaRequest):
                if isinstance(request.media, types.InputMediaUploadedPhoto):
                    return SimpleNamespace(photo=types.Photo(1, 2, b"ref", now, [], 2))
                documents.append(request.media)
                return SimpleNamespace(document=types.Document(20 + len(documents), 2, b"ref", now,
                                                               "video/mp4", 10, 2, []))
            if isinstance(request, functions.messages.SendMultiMediaRequest):
                return SimpleNamespace(updates=[
                    *[types.UpdateMessageID(id=n, random_id=item.random_id)
                      for n, item in zip((10, 11), request.multi_media)],
                    *[types.UpdateNewChannelMessage(types.Message(
                        id=n, peer_id=types.PeerChannel(1), grouped_id=99), 1, 1) for n in (10, 11)],
                ])
            raise AssertionError(type(request))
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"upload_concurrency": 2, "telegram_premium": True}
        uploader.peer = AsyncMock(return_value=types.InputPeerChannel(1, 2))
        uploader.call = telegram.FloodGate(call)
        with tempfile.TemporaryDirectory() as temp:
            files = {name: Path(temp) / (name + ".mp4") for name in ("video", "cover", "thumb", "preview")}
            for path in files.values():
                path.write_bytes(b"original")
            metadata = {"duration": 20, "width": 640, "height": 360}
            media = SimpleNamespace(**files, info=SimpleNamespace(title="完整视频"), video_meta=metadata, preview_meta=metadata)
            real_upload = telegram.upload_parts
            limits = []
            async def bounded(call, path, concurrency, *, max_bytes=3):
                limits.append(max_bytes)
                return await real_upload(call, path, concurrency, max_bytes=max_bytes)
            with patch.object(telegram, "MAX_VIDEO_SIZE", 3), \
                    patch.object(telegram, "PREMIUM_MAX_VIDEO_SIZE", 20), \
                    patch.object(telegram, "upload_parts", side_effect=bounded), \
                    patch.object(telegram_media, "split_points", new_callable=AsyncMock) as split, \
                    redirect_stdout(StringIO()):
                self.assertEqual(await uploader.upload(media, ["#ID123"]), [10, 11])
            self.assertTrue(limits)
            self.assertTrue(all(limit == 20 for limit in limits))
            split.assert_not_awaited()
        filename = next(a.file_name for a in documents[1].attributes if isinstance(a, types.DocumentAttributeFilename))
        self.assertEqual(filename, "video.mp4")


class SuccessHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def run_attempt(self, root, *, tg_success=True, quark_success=True, video_id="123"):
        async def prepare(_, directory, _config, **kwargs):
            video = directory / "video.mp4"
            video.write_bytes(b"original")
            return SimpleNamespace(video=video)
        tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(return_value=[10, 11]),
                             notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=MagicMock(return_value="quark-id"), close=MagicMock())
        if not tg_success:
            tg.upload.side_effect = common.JobError("电报失败")
        if not quark_success:
            q.upload.side_effect = common.JobError("夸克失败")
        with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                patch.object(source, "prepare_media", side_effect=prepare), \
                patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                redirect_stdout(StringIO()):
            code = await main.operate({"notify_saved_messages": False}, video_id=video_id)
        return code, self.capture.result(video_id)

    async def test_either_platform_success_records_status_and_actual_ids_in_d1(self):
        for tg_ok, q_ok in ((True, False), (False, True), (True, True), (False, False)):
            with self.subTest(telegram=tg_ok, quark=q_ok), tempfile.TemporaryDirectory() as temp:
                _, result = await self.run_attempt(Path(temp), tg_success=tg_ok, quark_success=q_ok)
                row = self.capture.row("123")
                self.assertEqual(row["telegram_status"], int(tg_ok))
                self.assertEqual(row["quark_status"], int(q_ok))
                self.assertEqual(row["telegram_message_ids"], "11" if tg_ok else "")
                self.assertEqual(row["quark_file_id"], "quark-id" if q_ok else "")
                self.assertTrue(result["db_recorded"])
                self.assertFalse((Path(temp) / ".state/uploaded-ids.json").exists())
                self.assertFalse((Path(temp) / ".state/result-123.json").exists())

    async def test_repeated_video_updates_one_record_and_preserves_snowflake_primary_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            await self.run_attempt(root)
            original_id = self.capture.row("123")["id"]
            await self.run_attempt(root)
            await self.run_attempt(root, video_id="456", tg_success=False)
            await self.run_attempt(root, tg_success=False, quark_success=False)
            self.assertEqual(len(self.capture.rows()), 2)
            self.assertEqual(self.capture.row("123")["id"], original_id)
            self.assertEqual(self.capture.row("123")["telegram_status"], 0)
            self.assertNotEqual(self.capture.row("456")["id"], original_id)

    async def test_d1_updates_before_the_other_upload_finishes(self):
        loop = asyncio.get_running_loop()
        recorded = asyncio.Event()
        release = threading.Event()
        platform = self.capture.records.platform
        async def prepare(_, directory, _config, **kwargs):
            video = directory / "video.mp4"
            video.write_bytes(b"original")
            return SimpleNamespace(video=video)
        def save(record_id, name, success, remote_ids):
            platform(record_id, name, success, remote_ids)
            if name == "telegram" and success:
                loop.call_soon_threadsafe(recorded.set)
        def delayed_quark(_media, **_kwargs):
            if not release.wait(5):
                raise AssertionError("测试没有释放夸克上传")
            raise common.JobError("另一端稍后失败")
        tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(return_value=[10, 11]),
                             notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=delayed_quark, close=MagicMock())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(self.capture.records, "platform", side_effect=save), \
                    patch.object(source, "prepare_media", side_effect=prepare), \
                    patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                    redirect_stdout(StringIO()):
                task = asyncio.create_task(main.operate({"notify_saved_messages": False}, video_id="123"))
                try:
                    await asyncio.wait_for(recorded.wait(), 3)
                    self.assertFalse(task.done())
                    self.assertEqual(self.capture.row("123")["telegram_message_ids"], "11")
                finally:
                    release.set()
                    await task

    async def test_database_failure_retains_media_then_retry_only_repairs_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(self.capture.records, "platform", side_effect=OSError("private token must not escape")):
                code, result = await self.run_attempt(root)
            self.assertEqual(code, 1)
            self.assertEqual(result["telegram"], "success")
            self.assertEqual(result["quark"], "success")
            self.assertFalse(result["db_recorded"])
            self.assertTrue(result["retained"])
            self.assertIn("D1记录未确认", result["notification"])
            self.assertNotIn("private token", json.dumps(result))
            task = TaskFiles.load(root, Path(result["retained_dir"]), retry=True)
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(telegram, "Telegram") as tg, patch.object(quark, "Quark") as q, \
                    redirect_stdout(StringIO()):
                self.assertEqual(await main.operate({"notify_saved_messages": False}, video_id="123", retained_task=task), 0)
            tg.assert_not_called()
            q.assert_not_called()
            self.assertTrue(self.capture.result("123")["cleaned"])
            self.assertEqual(self.capture.row("123")["telegram_status"], 1)
            self.assertEqual(self.capture.row("123")["quark_status"], 1)


if __name__ == "__main__":
    unittest.main()
