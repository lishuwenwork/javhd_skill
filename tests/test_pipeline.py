from __future__ import annotations

import asyncio
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common
import main
import quark
import source
import telegram
from telethon import errors, functions, types
import httpx
from d1_testing import install_capture

PAGE = "https://javhd.com/zh/id/123"
HTML = '''<h1>示例中文标题</h1><meta property="og:image" content="https://cdn.example/cover.jpg">
<player-component content-path="/zh/player_api?videoId=7&amp;is_trailer=0"></player-component>
<thumb-component link-content="/zh/id/456" video-preview="https://cdn.example/wrong.mp4"></thumb-component>'''


class InputTests(unittest.TestCase):
    def setUp(self):
        self.capture = install_capture(self)

    def test_valid_tags_and_deduplication(self):
        self.assertEqual(common.parse_input("id:123 标签:#4k、#高清 #4k"), ("123", ["#4k", "#高清"]))

    def test_tags_are_optional(self):
        for text in ("id:123", "id:123 标签:", "id:123 标签:   "):
            with self.subTest(text=text):
                self.assertEqual(common.parse_input(text), ("123", []))

    def test_numeric_tags_use_uppercase_id_prefix(self):
        self.assertEqual(common.parse_tags("#123"), ["#ID123"])
        self.assertEqual(common.parse_input("id:64584 标签:#123、#4k"), ("64584", ["#ID123", "#4k"]))
        self.assertEqual(common.parse_tags("#123 #456 #123"), ["#ID123", "#ID456"])
        self.assertEqual(common.parse_tags("#ID123 #123"), ["#ID123"])
        self.assertEqual(common.parse_tags("#id123"), ["#id123"])
        self.assertEqual(common.parse_tags("#4k"), ["#4k"])

    def test_id_tag_always_prepended_without_duplicates(self):
        self.assertEqual(common.tags_with_id("64584", []), ["#ID64584"])
        self.assertEqual(common.tags_with_id("64584", ["#4k"]), ["#ID64584", "#4k"])
        self.assertEqual(common.tags_with_id("64584", ["#ID64584", "#4k"]), ["#ID64584", "#4k"])
        self.assertEqual(common.tags_with_id("123", ["#123", "#高清", "#ID123"]), ["#ID123", "#高清"])
        self.assertEqual(common.tags_with_id("123", ["#id123"]), ["#ID123", "#id123"])

    def test_id_caption_uses_uppercase_prefix(self):
        caption, entities = common.caption_and_tags("标题", common.tags_with_id("123", ["#4k"]))
        self.assertEqual(caption, "标题\n\n#ID123 #4k")
        self.assertEqual([(entity.offset, entity.length) for entity in entities], [(4, 6), (11, 3)])

    def test_invalid_input_only_prints_format_hint(self):
        for text in ("123", "id:-1 标签:#高清", "id:0 标签:#高清", "id:12 标签:高清", "id:12 标签:#高清;whoami"):
            with self.subTest(text=text), redirect_stdout(StringIO()) as out:
                self.assertEqual(main.main(["--input", text]), 2)
                self.assertEqual(out.getvalue(), common.FORMAT_HINT + "\n")

    def test_busy_task_does_not_leave_stale_success_result(self):
        from filelock import FileLock
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "STATE", Path(temp)), \
                patch.object(main, "load_config", return_value={}), redirect_stdout(StringIO()):
            result_path = Path(temp) / "result-123.json"
            result_path.write_text('{"telegram":"success","quark":"success"}', encoding="utf-8")
            with FileLock(str(Path(temp) / "task.lock")):
                self.assertEqual(main.main(["--id", "123", "--tags", "#高清"]), 1)
            result = self.capture.result("123")
            self.assertEqual(result["telegram"], "failed")
            self.assertEqual(result["quark"], "failed")

    def test_utf16_hashtag_entities(self):
        caption, entities = common.caption_and_tags("中文😀标题", ["#4k", "#高清"])
        encoded = caption.encode("utf-16-le")
        self.assertEqual([encoded[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
                          for e in entities], ["#4k", "#高清"])
        self.assertTrue(all(isinstance(e, types.MessageEntityHashtag) for e in entities))

    def test_long_caption_preserves_tags(self):
        caption, _ = common.caption_and_tags("中文😀" * 1000, ["#4k", "#高清"])
        self.assertLessEqual(common.utf16_length(caption), 1024)
        self.assertTrue(caption.endswith("#4k #高清"))

    def test_native_crypto_enabled(self):
        self.assertTrue(common.check_crypto()["native_enabled"])

    def test_qr_image_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(common, "STATE", Path(temp)), redirect_stdout(StringIO()):
            path = common.show_qr("telegram", "tg://login?token=test-only")
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            common.login_status("telegram", "ready")
            self.assertFalse(path.exists())


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def test_native_endpoint_and_no_recommendation_preview(self):
        data, endpoint = source.parse_detail(HTML, PAGE, "123")
        self.assertEqual(endpoint, "https://javhd.com/zh/player_api?videoId=7&is_trailer=0")
        self.assertEqual(data["title"], "示例中文标题")
        self.assertEqual(data["preview_url"], "")

    def test_trailer_page_selects_full_endpoint_but_trailer_sources_are_rejected(self):
        _, endpoint = source.parse_detail(HTML.replace("is_trailer=0", "is_trailer=1"), PAGE, "123")
        self.assertEqual(source.full_player_endpoint(endpoint),
                         "https://javhd.com/zh/player_api?videoId=7&is_trailer=0")
        with self.assertRaises(common.JobError):
            source.best_source({"sources": [{"src": "https://cdn.example/trailers/a.mp4"}]}, PAGE)

    def test_full_endpoint_keeps_internal_id_legacy_route_and_extra_parameters(self):
        endpoint = "https://javhd.com/zh/player/120378?token=abc&is_trailer=1&lang=zh"
        self.assertEqual(source.full_player_endpoint(endpoint),
                         "https://javhd.com/zh/player/120378?token=abc&lang=zh&is_trailer=0")

    def test_high_resolution_trailer_and_hls_do_not_hide_full_mp4(self):
        data = {"sources": [{"src": "https://cdn.example/trailers/a.mp4", "res": 2160},
                            {"src": "https://cdn.example/a.m3u8", "res": 1080},
                            {"src": "https://cdn.example/full.mp4", "res": 720}]}
        self.assertEqual(source.best_source(data, PAGE), "https://cdn.example/full.mp4")

    def test_source_quality_selection_and_placeholder(self):
        data = {"sources": [{"src": "https://cdn.example/480.mp4", "res": 480},
                            {"src": "https://cdn.example/1080.mp4", "res": "1080p"},
                            {"src": "https://cdn.example/black_cap.mp4", "res": 2160}]}
        self.assertEqual(source.best_source(data, PAGE), "https://cdn.example/1080.mp4")

    def test_cross_origin_endpoint_and_non_chinese_title_rejected(self):
        with self.assertRaises(common.JobError):
            source.parse_detail(HTML.replace("/zh/player_api", "https://other.example/zh/player_api"), PAGE, "123")
        with self.assertRaises(common.JobError):
            source.parse_detail(HTML.replace("示例中文标题", "English title"), PAGE, "123")

    async def test_download_checks_declared_length_and_deletes_partial(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"short", headers={"Content-Length": "99", "Content-Type": "video/mp4"}))
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "asset" / "video.mp4"
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(common.JobError):
                    await source.download(client, "https://cdn.example/full.mp4", target, referer=PAGE, video=True)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    async def test_html_is_not_saved_as_video(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="login page"))
        with tempfile.TemporaryDirectory() as temp:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(common.JobError):
                    await source.download(client, PAGE, Path(temp) / "asset" / "v.mp4", referer=PAGE, video=True)

    async def test_resolve_uses_only_original_endpoint(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            if request.url.path == "/zh/id/123":
                return httpx.Response(200, text=HTML)
            if request.url.path == "/share2/trailers/123/5sec_1000_n.mp4":
                return httpx.Response(404)
            return httpx.Response(200, json={"sources": [{"src": "https://cdn.example/full.mp4", "res": 1080}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            info = await source.resolve(client, "123")
        self.assertEqual(info.video_url, "https://cdn.example/full.mp4")
        self.assertEqual(info.preview_url, "")
        self.assertEqual(len(requests), 4)
        self.assertTrue(requests[1].endswith("videoId=7&is_trailer=0"))
        self.assertTrue(requests[-1].endswith("videoId=7&is_trailer=1"))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_sliding_window_replenishes_without_waiting_for_batch(self):
        started, waits = [], {}
        inflight = maximum = 0
        auto = False
        async def call(request):
            nonlocal inflight, maximum
            index = request.file_part
            started.append(index)
            inflight += 1
            maximum = max(maximum, inflight)
            try:
                if not auto:
                    waits[index] = asyncio.Event()
                    await waits[index].wait()
                else:
                    await asyncio.sleep(0)
                return True
            finally:
                inflight -= 1
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            content = b"a" * (telegram.PART_SIZE * 6 + 17)
            path.write_bytes(content)
            task = asyncio.create_task(telegram.upload_parts(call, path))
            for _ in range(100):
                if len(started) == telegram.DEFAULT_CONCURRENCY:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(started, list(range(telegram.DEFAULT_CONCURRENCY)))
            waits[1].set()
            for _ in range(100):
                if len(started) == telegram.DEFAULT_CONCURRENCY + 1:
                    break
                await asyncio.sleep(0.001)
            # 其余分块还没有确认，下一个分块已经开始，不必等整批。
            self.assertEqual(started, list(range(telegram.DEFAULT_CONCURRENCY + 1)))
            auto = True
            for event in waits.values():
                event.set()
            handle = await asyncio.wait_for(task, 2)
            self.assertLessEqual(maximum, telegram.DEFAULT_CONCURRENCY)
            self.assertEqual(handle.md5_checksum, hashlib.md5(content).hexdigest())
            self.assertEqual(handle.parts, 7)

    async def test_large_file_uses_512_kib_big_parts(self):
        requests = []
        async def call(request):
            requests.append(request)
            await asyncio.sleep(0)
            return True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            path.write_bytes(b"a" * (11 * 1024 * 1024 + 3))
            handle = await telegram.upload_parts(call, path)
        self.assertIsInstance(handle, types.InputFileBig)
        self.assertEqual(len(requests), 23)
        self.assertTrue(all(isinstance(r, functions.upload.SaveBigFilePartRequest) for r in requests))
        self.assertTrue(all(len(r.bytes) == telegram.PART_SIZE for r in requests[:-1]))
        self.assertEqual(len(requests[-1].bytes), 3)
        self.assertEqual(len({r.file_id for r in requests}), 1)

    async def test_failed_part_stops_pipeline_without_retry(self):
        requests = []
        async def call(request):
            requests.append(request.file_part)
            if request.file_part == 1:
                return False
            await asyncio.sleep(0.01)
            return True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            path.write_bytes(b"a" * telegram.PART_SIZE * 20)
            with self.assertRaises(common.JobError):
                await telegram.upload_parts(call, path)
            # 失败的分块只发送一次，不静默重试。
            self.assertEqual(requests.count(1), 1)
            self.assertLess(len(requests), 20)
            path.unlink()  # Windows 也能删除，说明没有残留的在途任务仍占用文件。

    async def test_in_flight_window_is_capped_even_if_concurrency_is_high(self):
        active = maximum = 0
        release = asyncio.Event()
        started = asyncio.Event()
        async def call(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == telegram.MAX_IN_FLIGHT:
                started.set()
            try:
                await release.wait()
                return True
            finally:
                active -= 1
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            path.write_bytes(b"a" * telegram.PART_SIZE * 16)
            task = asyncio.create_task(telegram.upload_parts(call, path, 16))
            await asyncio.wait_for(started.wait(), 2)
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(maximum, telegram.MAX_IN_FLIGHT)

    async def test_concurrency_is_fully_used(self):
        active = maximum = 0
        release = asyncio.Event()
        started = asyncio.Event()
        async def call(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == telegram.DEFAULT_CONCURRENCY:
                started.set()
            try:
                await release.wait()
                return True
            finally:
                active -= 1
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            path.write_bytes(b"a" * telegram.PART_SIZE * 12)
            task = asyncio.create_task(telegram.upload_parts(call, path))
            await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(maximum, telegram.DEFAULT_CONCURRENCY)
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertGreaterEqual(telegram.DEFAULT_CONCURRENCY, 6)

    async def test_cancel_stops_all_in_flight_parts(self):
        active = 0
        started = asyncio.Event()
        async def call(request):
            nonlocal active
            active += 1
            if active == telegram.DEFAULT_CONCURRENCY:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            path.write_bytes(b"a" * telegram.PART_SIZE * 12)
            task = asyncio.create_task(telegram.upload_parts(call, path))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(active, 0)

    async def test_file_changed_during_upload_is_rejected(self):
        async def call(request):
            if request.file_part == 0:
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            return True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v.mp4"
            path.write_bytes(b"a" * telegram.PART_SIZE * 3)
            with self.assertRaises(common.JobError):
                await telegram.upload_parts(call, path)

    async def test_floodwait_waits_and_retries_same_request(self):
        client = AsyncMock(side_effect=[errors.FloodWaitError(request=None, capture=2), True])
        gate = telegram.FloodGate(client)
        clock = SimpleNamespace(value=100.0)
        loop = SimpleNamespace(time=lambda: clock.value)
        async def sleep(delay):
            clock.value += delay
        request = object()
        with patch.object(telegram.asyncio, "get_running_loop", return_value=loop), \
                patch.object(telegram.asyncio, "sleep", side_effect=sleep) as sleeper:
            self.assertTrue(await gate(request))
        self.assertEqual(sleeper.call_args.args[0], 3)
        self.assertEqual(client.await_count, 2)
        self.assertIs(client.call_args.args[0], request)

    async def test_album_cover_and_clickable_tags(self):
        requests, documents = [], []
        now = datetime.now(timezone.utc)
        async def call(request):
            requests.append(request)
            if isinstance(request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)):
                return True
            if isinstance(request, functions.messages.UploadMediaRequest):
                if isinstance(request.media, types.InputMediaUploadedPhoto):
                    return SimpleNamespace(photo=types.Photo(1, 2, b"ref", now, [], 2))
                documents.append(request.media)
                return SimpleNamespace(document=types.Document(10 + len(documents), 2, b"ref", now,
                                                               "video/mp4", 10, 2, []))
            if isinstance(request, functions.messages.SendMultiMediaRequest):
                # 真实响应同时包含 UpdateMessageID，用 random_id 关联到消息。
                return SimpleNamespace(updates=[
                    *[types.UpdateMessageID(id=n, random_id=item.random_id)
                      for n, item in zip((10, 11), request.multi_media)],
                    *[types.UpdateNewChannelMessage(
                        types.Message(id=n, peer_id=types.PeerChannel(1), grouped_id=99), 1, 1)
                      for n in (10, 11)]])
            raise AssertionError(type(request))
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"upload_concurrency": 4}
        uploader.client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)))
        uploader.peer = AsyncMock(return_value=types.InputPeerChannel(1, 2))
        uploader.call = telegram.FloodGate(call)
        with tempfile.TemporaryDirectory() as temp:
            files = {}
            for name in ("video", "cover", "thumb", "preview"):
                files[name] = Path(temp) / (name + ".mp4")
                files[name].write_bytes(b"test")
            media = SimpleNamespace(**files, info=SimpleNamespace(title="示例中文标题"),
                                    video_meta={"width": 640, "height": 360, "duration": 20},
                                    preview_meta={"width": 480, "height": 270, "duration": 5})
            self.assertEqual(await uploader.upload(media, ["#4k", "#高清"]), [10, 11])
        album = next(r for r in requests if isinstance(r, functions.messages.SendMultiMediaRequest))
        self.assertEqual(len(album.multi_media), 2)
        self.assertIsNotNone(documents[1].video_cover)
        self.assertIsNotNone(documents[1].thumb)
        self.assertIsNotNone(album.multi_media[1].media.video_cover)
        self.assertTrue(documents[0].nosound_video)
        self.assertFalse(any(isinstance(a, types.DocumentAttributeAnimated) for d in documents for a in d.attributes))
        self.assertTrue(album.multi_media[1].message.endswith("#4k #高清"))
        self.assertEqual(len(album.multi_media[1].entities), 2)
        self.assertEqual(len({item.random_id for item in album.multi_media}), 2)

    async def test_album_without_matching_random_id_is_not_reported_as_success(self):
        now = datetime.now(timezone.utc)
        async def call(request):
            if isinstance(request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)):
                return True
            if isinstance(request, functions.messages.UploadMediaRequest):
                if isinstance(request.media, types.InputMediaUploadedPhoto):
                    return SimpleNamespace(photo=types.Photo(1, 2, b"ref", now, [], 2))
                return SimpleNamespace(document=types.Document(10, 2, b"ref", now, "video/mp4", 10, 2, []))
            if isinstance(request, functions.messages.SendMultiMediaRequest):
                # 只有消息更新、没有 random_id 关联，不能据此认定本次发送成功。
                return SimpleNamespace(updates=[types.UpdateNewChannelMessage(
                    types.Message(id=n, peer_id=types.PeerChannel(1), grouped_id=99), 1, 1) for n in (10, 11)])
            raise AssertionError(type(request))
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"upload_concurrency": 3}
        uploader.client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)))
        uploader.peer = AsyncMock(return_value=types.InputPeerChannel(1, 2))
        uploader.call = telegram.FloodGate(call)
        with tempfile.TemporaryDirectory() as temp:
            files = {name: Path(temp) / f"{name}.mp4" for name in ("video", "cover", "thumb", "preview")}
            for path in files.values():
                path.write_bytes(b"test")
            media = SimpleNamespace(**files, info=SimpleNamespace(title="示例中文标题"),
                                    video_meta={"width": 640, "height": 360, "duration": 20},
                                    preview_meta={"width": 480, "height": 270, "duration": 5})
            with self.assertRaises(common.JobError):
                await uploader.upload(media, ["#ID123"])


class QuarkTests(unittest.TestCase):
    def test_official_cli_upload_requires_final_file_id(self):
        client = quark.Quark()
        client.folder = MagicMock(return_value="target")
        fake = MagicMock()
        fake.run.return_value = (1, [{"code": -204, "msg": "上传操作失败", "action": "upload",
                                      "type": "result", "data": {}}], "")
        client.cli = fake
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "示例中文标题.mp4"
            path.write_bytes(b"test")
            with self.assertRaises(common.JobError):
                client.upload(SimpleNamespace(video=path))
            self.assertEqual(path.read_bytes(), b"test")
        self.assertEqual(client.folder.call_count, 1)

    def test_folder_path_is_resolved_before_upload(self):
        client = quark.Quark()
        client.cli = MagicMock()
        client.cli.run.return_value = (0, [{"code": 0, "msg": "成功", "action": "create-folder",
                                           "type": "result", "data": {"fid": "javhd"}}], "")
        self.assertEqual(client.folder("JAVHD"), "javhd")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def exercise(self, failed=None, tags=("#高清",)):
        seen_paths = []
        async def prepare(video_id, directory, config, **kwargs):
            path = directory / "video" / "示例中文标题.mp4"
            path.parent.mkdir()
            path.write_bytes(b"test")
            seen_paths.append(path)
            return SimpleNamespace(video=path)
        tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(return_value=[10, 11]),
                             notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=MagicMock(return_value="quark-file"), close=MagicMock())
        if failed == "quark":
            q.upload.side_effect = RuntimeError("sensitive title and URL must not escape")
        if failed == "telegram":
            tg.upload.side_effect = RuntimeError("sensitive title and URL must not escape")
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "ROOT", Path(temp)), \
                patch.object(main, "STATE", Path(temp) / ".state"), \
                patch.object(source, "prepare_media", side_effect=prepare), \
                patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                redirect_stdout(StringIO()):
            code = await main.operate({"notify_saved_messages": True}, video_id="123", tags=list(tags))
            result = self.capture.result("123")
            self.assertTrue(seen_paths)
            self.assertEqual(seen_paths[0].exists(), bool(failed))
            if failed:
                self.assertTrue(result["retained"])
                self.assertTrue(result["awaiting_decision"])
                self.assertEqual(Path(result["retained_dir"]), seen_paths[0].parent.parent)
                self.assertIn("等待选择删除或重试", result["notification"])
                self.assertNotIn("本地清理失败", result["notification"])
        self.assertEqual(result["cleaned"], not bool(failed))
        self.assertTrue(result["saved_message"])
        self.assertNotIn("敏感", result["notification"])
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertNotIn("标题", result["notification"])
        self.assertNotIn("http", result["notification"])
        self.assertEqual(tg.notify.call_args.args[0], result["notification"])
        return code, result, tg

    async def test_both_success_cleanup_and_minimal_notification(self):
        code, result, _ = await self.exercise()
        self.assertEqual(code, 0)
        self.assertEqual(result["notification"], "id:123 上传成功", result["errors"])

    async def test_telegram_always_receives_id_hashtag(self):
        _, _, tg = await self.exercise()
        self.assertEqual(tg.upload.call_args.args[1], ["#ID123", "#高清"])
        _, _, tg = await self.exercise(tags=())
        self.assertEqual(tg.upload.call_args.args[1], ["#ID123"])

    async def test_one_failure_does_not_hide_other_success(self):
        for failed in ("telegram", "quark"):
            code, result, _ = await self.exercise(failed)
            self.assertEqual(code, 1)
            self.assertEqual(result[failed], "failed")
            self.assertEqual(result["quark" if failed == "telegram" else "telegram"], "success")

    async def test_async_uploads_really_overlap(self):
        started = set()
        loop = asyncio.get_running_loop()
        both = asyncio.Event()
        async def tg():
            started.add("telegram")
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
        def q():
            loop.call_soon_threadsafe(started.add, "quark")
            loop.call_soon_threadsafe(both.set)
            return True
        results = await main.settle(tg(), asyncio.to_thread(q))
        self.assertEqual(started, {"telegram", "quark"})
        self.assertFalse(any(isinstance(r, BaseException) for r in results))


if __name__ == "__main__":
    unittest.main()
