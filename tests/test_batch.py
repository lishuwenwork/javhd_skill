"""按需解析的双队列：下载串行、上传串行、阶段交叠及失败/中止后的文件保留。"""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import JobError, VideoRequest
import main
import quark
import source
import telegram
from task_files import TaskFiles
from test_retained_tasks import media_files
from d1_testing import install_capture

CONFIG = {"notify_saved_messages": False, "telegram_channel_id": -100123}


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def test_download_is_serial_upload_is_serial_but_stages_overlap_with_one_lookahead(self):
        events = []
        first_download = asyncio.Event()
        release_download = asyncio.Event()
        second_download_done = asyncio.Event()
        first_upload = asyncio.Event()
        release_upload = asyncio.Event()
        counts = {"download": 0, "upload": 0}
        async def prepare(video_id, directory, config, **kwargs):
            counts["download"] += 1
            self.assertEqual(counts["download"], 1)
            events.append("resolve-" + video_id)
            if video_id == "1":
                first_download.set()
                await release_download.wait()
            if video_id == "2":
                await first_upload.wait()
                self.assertEqual(counts["upload"], 1)
                second_download_done.set()
            media = media_files(directory, video_id)
            events.append("downloaded-" + video_id)
            counts["download"] -= 1
            return media
        async def upload(config, *, video_id, prepared, **kwargs):
            self.assertIsNotNone(prepared.media)
            self.assertFalse(prepared.task.data["quark_upload"]["attempted"])
            counts["upload"] += 1
            self.assertEqual(counts["upload"], 1)
            events.append("upload-" + video_id)
            if video_id == "1":
                first_upload.set()
                await release_upload.wait()
            events.append("uploaded-" + video_id)
            counts["upload"] -= 1
            prepared.result = {"id": video_id, "telegram": "success", "quark": "success"}
            return 0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(source, "prepare_media", side_effect=prepare), \
                    patch.object(main, "operate", side_effect=upload), redirect_stdout(StringIO()):
                runner = asyncio.create_task(main.operate_batch(CONFIG, [VideoRequest(str(n), []) for n in range(1, 5)]))
                try:
                    await asyncio.wait_for(first_download.wait(), 2)
                    self.assertEqual(events, ["resolve-1"])
                    release_download.set()
                    await asyncio.wait_for(second_download_done.wait(), 2)
                    await asyncio.sleep(0)
                    self.assertNotIn("resolve-3", events)
                    self.assertNotIn("upload-2", events)
                    self.assertFalse(runner.done())
                finally:
                    release_download.set()
                    release_upload.set()
                self.assertEqual(await asyncio.wait_for(runner, 3), 0)
            for n in range(1, 4):
                self.assertLess(events.index(f"downloaded-{n}"), events.index(f"resolve-{n+1}"))
                self.assertLess(events.index(f"uploaded-{n}"), events.index(f"upload-{n+1}"))
            self.assertLess(events.index("upload-1"), events.index("downloaded-2"))
            self.assertLess(events.index("downloaded-2"), events.index("uploaded-1"))
            self.assertLess(events.index("uploaded-1"), events.index("resolve-3"))


class BatchLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def test_platforms_for_same_video_remain_parallel_and_batch_cleans_each_success(self):
        loop = asyncio.get_running_loop()
        tg_started = {str(n): threading.Event() for n in (1, 2, 3)}
        q_started = {str(n): asyncio.Event() for n in (1, 2, 3)}
        seen = []
        async def prepare(video_id, directory, config, **kwargs):
            seen.append(("prepare", video_id, kwargs.get("page_url", "")))
            return media_files(directory, video_id)
        async def upload_tg(media, tags):
            video_id = media.info.video_id
            seen.append(("tg", video_id))
            self.assertEqual(tags, [f"#ID{video_id}", "#4k"])
            tg_started[video_id].set()
            await asyncio.wait_for(q_started[video_id].wait(), 2)
            return [100 + int(video_id), 10 + int(video_id)]
        def upload_q(media, **kwargs):
            video_id = media.info.video_id
            self.assertFalse(kwargs["retry"])
            self.assertFalse(kwargs["checkpoint"]["attempted"])
            self.assertTrue(tg_started[video_id].wait(2))
            seen.append(("q", video_id))
            loop.call_soon_threadsafe(q_started[video_id].set)
            return "q-" + video_id
        tg = SimpleNamespace(login=AsyncMock(), upload=upload_tg, notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=upload_q, close=MagicMock())
        requests = [VideoRequest("1", ["#4k"], "https://javhd.com/zh/custom-first"),
                    VideoRequest("2", ["#4k"]), VideoRequest("3", ["#4k"])]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(source, "prepare_media", side_effect=prepare), \
                    patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                    redirect_stdout(StringIO()):
                self.assertEqual(await asyncio.wait_for(main.operate_batch(CONFIG, requests), 5), 0)
            summary = self.capture.result(event="batch_result")
            self.assertEqual(summary["success_count"], 3)
            self.assertEqual(summary["failed_count"], 0)
            self.assertEqual([item["id"] for item in summary["results"]], ["1", "2", "3"])
            for video_id in ("1", "2", "3"):
                result = self.capture.result(video_id)
                self.assertTrue(result["cleaned"])
                self.assertTrue(result["db_recorded"])
            self.assertEqual([row["video_id"] for row in self.capture.rows()], ["1", "2", "3"])
            self.assertEqual(list((root / "downloads").iterdir()), [])
            self.assertEqual([entry for entry in seen if entry[0] == "prepare"], [
                ("prepare", "1", requests[0].page_url), ("prepare", "2", ""), ("prepare", "3", "")])
            for video_id in ("2", "3"):
                before = str(int(video_id) - 1)
                self.assertLess(seen.index(("q", before)), seen.index(("tg", video_id)))
            self.assertEqual(q.close.call_count, 3)
            self.assertEqual(tg.close.await_count, 3)

    async def test_failed_download_and_failed_upload_keep_their_files_and_continue_batch(self):
        prepared = {}
        async def prepare(video_id, directory, config, **kwargs):
            if video_id == "1":
                path = directory / "downloaded-before-error.bin"
                path.write_bytes(b"retain")
                prepared[video_id] = path
                raise JobError("download preparation failed")
            media = media_files(directory, video_id)
            prepared[video_id] = media.video
            return media
        async def upload_tg(media, tags):
            return [100 + int(media.info.video_id), 10 + int(media.info.video_id)]
        def upload_q(media, **kwargs):
            if media.info.video_id == "2":
                raise JobError("network failure")
            return "q-" + media.info.video_id
        tg = SimpleNamespace(login=AsyncMock(), upload=upload_tg, notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=upload_q, close=MagicMock())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(source, "prepare_media", side_effect=prepare), \
                    patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                    redirect_stdout(StringIO()):
                self.assertEqual(await main.operate_batch(CONFIG, [VideoRequest(str(n), []) for n in (1, 2, 3)]), 1)
            summary = self.capture.result(event="batch_result")
            self.assertEqual(summary["failed_count"], 2)
            self.assertEqual(summary["success_count"], 1)
            self.assertTrue(prepared["1"].exists())
            self.assertTrue(prepared["2"].exists())
            self.assertFalse(prepared["3"].exists())
            for result in summary["results"][:2]:
                self.assertTrue(result["awaiting_decision"])
                task = TaskFiles.load(root, Path(result["retained_dir"]), retry=True)
                self.assertEqual(task.data["state"], "retained")
            self.assertEqual(tg.login.await_count, 2)
            self.assertEqual([row["video_id"] for row in self.capture.rows() if row["telegram_status"] or row["quark_status"]], ["2", "3"])

    async def test_cancel_stops_future_resolution_waits_for_upload_and_retains_second_task(self):
        for second_ready in (False, True):
            with self.subTest(second_ready=second_ready), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                loop = asyncio.get_running_loop()
                second_started = asyncio.Event()
                second_cancelled = asyncio.Event()
                q_started = asyncio.Event()
                release_q = threading.Event()
                seen, paths = [], {}
                async def prepare(video_id, directory, config, **kwargs):
                    seen.append(video_id)
                    media = media_files(directory, video_id)
                    paths[video_id] = media.video
                    if video_id == "2":
                        second_started.set()
                        if not second_ready:
                            try:
                                await asyncio.Event().wait()
                            except asyncio.CancelledError:
                                second_cancelled.set()
                                raise
                    return media
                def upload_q(media, **kwargs):
                    self.assertEqual(media.info.video_id, "1")
                    loop.call_soon_threadsafe(q_started.set)
                    self.assertTrue(release_q.wait(5))
                    self.assertTrue(media.video.is_file())
                    return "q-1"
                tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(return_value=[10, 11]),
                                     notify=AsyncMock(), close=AsyncMock())
                q = SimpleNamespace(login=MagicMock(), upload=upload_q, close=MagicMock())
                with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                        patch.object(source, "prepare_media", side_effect=prepare), \
                        patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                        redirect_stdout(StringIO()):
                    runner = asyncio.create_task(main.operate_batch(CONFIG, [VideoRequest(str(n), []) for n in (1, 2, 3)]))
                    try:
                        await asyncio.wait_for(second_started.wait(), 2)
                        await asyncio.wait_for(q_started.wait(), 2)
                        runner.cancel()
                        if not second_ready:
                            await asyncio.wait_for(second_cancelled.wait(), 2)
                        else:
                            await asyncio.sleep(0)
                        self.assertFalse(runner.done())
                        self.assertTrue(paths["1"].exists())
                    finally:
                        release_q.set()
                    self.assertEqual(await asyncio.wait_for(runner, 3), 130)
                summary = self.capture.result(event="batch_result")
                self.assertTrue(summary["interrupted"])
                self.assertEqual(summary["skipped_count"], 1)
                self.assertEqual(seen, ["1", "2"])
                self.assertFalse(paths["1"].exists())
                self.assertTrue(paths["2"].exists())
                pending = summary["results"][1]
                task = TaskFiles.load(root, Path(pending["retained_dir"]), retry=True)
                self.assertFalse(task.data["quark_upload"]["attempted"])
                self.assertEqual(task.restore_media() is not None, second_ready)
                self.assertEqual(tg.upload.await_count, 1)


class BatchLockTests(unittest.TestCase):
    def setUp(self):
        self.capture = install_capture(self)

    def test_global_lock_rejects_whole_batch_and_never_starts_parallel_command(self):
        from filelock import FileLock
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with patch.object(main, "STATE", state), patch.object(main, "load_config", return_value=CONFIG), \
                    patch.object(main, "operate_batch") as batch, redirect_stdout(StringIO()), \
                    FileLock(str(state / "task.lock")):
                self.assertEqual(main.main(["--ids", "123", "456"]), 1)
            batch.assert_not_called()
            summary = self.capture.result(event="batch_result")
            self.assertEqual(summary["failed_count"], 2)
            for video_id in ("123", "456"):
                result = self.capture.result(video_id)
                self.assertEqual(result["errors"]["startup"], "已有任务运行")


if __name__ == "__main__":
    unittest.main()
