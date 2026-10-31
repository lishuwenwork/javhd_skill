"""失败保留、明确决策后的重试/删除，以及任务目录边界；不连接外部服务。"""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import stat
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import JobError
import common
import main
import quark
import source
import telegram
import telegram_media
from task_files import MANIFEST, TaskFiles
from d1_testing import install_capture

CONFIG = {"telegram_channel_id": -1001112223334, "notify_saved_messages": True}


class AtomicStateTests(unittest.TestCase):
    def test_brief_file_lock_preserves_old_state_until_new_state_is_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")
            original_replace = Path.replace
            blocked = False
            def replace(source, target):
                nonlocal blocked
                if target == path and not blocked:
                    blocked = True
                    self.assertEqual(json.loads(path.read_text()), {"old": True})
                    raise PermissionError("simulated temporary sharing conflict")
                return original_replace(source, target)
            with patch.object(Path, "replace", replace), patch.object(common.time, "sleep"):
                common.save_json(path, {"new": True})
            self.assertTrue(blocked)
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_permanent_file_lock_keeps_previous_state_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")
            with patch.object(Path, "replace", side_effect=PermissionError("locked")), \
                    patch.object(common.time, "sleep"):
                with self.assertRaises(PermissionError):
                    common.save_json(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])


def media_files(directory, video_id="123"):
    files = {}
    for name in ("video", "cover", "thumb", "preview"):
        path = directory / name / ("敏感中文标题.mp4" if name == "video" else name + ".bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "-original").encode())
        files[name] = path
    info = source.VideoInfo(video_id, "敏感中文标题", "https://secret.invalid/page", "", "",
                            "https://secret.invalid/video?signature=private")
    metadata = {"duration": 20, "width": 640, "height": 360}
    return source.MediaFiles(info=info, **files, video_meta=metadata, preview_meta=metadata)


class RetainedTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def attempt(self, root, *, tg_success=True, quark_success=True, notify_failure=False):
        prepared = []
        async def prepare(video_id, directory, _config, **kwargs):
            media = media_files(directory, video_id)
            prepared.append(media)
            return media
        async def upload_tg(media, _tags):
            self.assertTrue(media.retain_work_files)
            part = media.video.parent / "telegram-parts-test" / "part(1).mp4"
            part.parent.mkdir()
            part.write_bytes(b"telegram-part")
            if not tg_success:
                raise JobError("电报结果未确认")
            return [10, 11]
        def upload_quark(media, **kwargs):
            self.assertEqual(media.video.read_bytes(), b"video-original")
            metadata = media.video.stat()
            kwargs["checkpoint_callback"]({"version": 1, "attempted": True, "file_name": media.video.name,
                "file_size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns, "record_id": "retained-record",
                "previous_ids": [], "parent_fid": "target", "session_id": "1234567890-abcdef", "started_at": 1})
            if not quark_success:
                raise JobError("夸克结果未确认")
            return "quark-file"
        tg = SimpleNamespace(login=AsyncMock(), upload=upload_tg, notify=AsyncMock(), close=AsyncMock())
        if notify_failure:
            tg.notify.side_effect = OSError("notification unavailable")
        q = SimpleNamespace(login=MagicMock(), upload=upload_quark, close=MagicMock())
        with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                patch.object(source, "prepare_media", side_effect=prepare), \
                patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                redirect_stdout(StringIO()):
            code = await main.operate(CONFIG, video_id="123", tags=["#4k"])
        result = self.capture.result("123")
        return code, result, prepared[0]

    async def command(self, root, args, *, tg=None, q=None, prepare=None):
        tg_factory = MagicMock(return_value=tg) if tg else MagicMock(side_effect=AssertionError("不得重建已成功的电报端"))
        q_factory = MagicMock(return_value=q) if q else MagicMock(side_effect=AssertionError("不得重建已成功的夸克端"))
        prepare = prepare or AsyncMock(side_effect=AssertionError("不能重新下载已有完整媒体"))
        with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                patch.object(main, "load_config", return_value=CONFIG), \
                patch.object(source, "prepare_media", prepare), \
                patch.object(telegram, "Telegram", tg_factory), patch.object(quark, "Quark", q_factory), \
                redirect_stdout(StringIO()):
            code = await asyncio.to_thread(main.main, args)
        return code, tg_factory, q_factory, prepare

    async def test_both_success_delete_original_parts_and_task_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code, result, media = await self.attempt(root)
            self.assertEqual(code, 0)
            self.assertTrue(result["cleaned"])
            self.assertFalse(result["retained"])
            self.assertFalse(result["awaiting_decision"])
            self.assertFalse(media.video.exists())
            self.assertEqual(list((root / "downloads").iterdir()), [])
            self.assertIsNotNone(self.capture.row("123"))

    async def test_any_upload_failure_keeps_all_media_and_prompts_for_decision(self):
        for tg_ok, q_ok in ((True, False), (False, True), (False, False)):
            with self.subTest(telegram=tg_ok, quark=q_ok), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                code, result, media = await self.attempt(root, tg_success=tg_ok, quark_success=q_ok)
                self.assertEqual(code, 1)
                self.assertFalse(result["cleaned"])
                self.assertTrue(result["retained"])
                self.assertTrue(result["awaiting_decision"])
                self.assertTrue(result["retry_ready"])
                self.assertEqual(result["next_actions"], ["retry", "delete"])
                self.assertIn("等待选择删除或重试", result["notification"])
                self.assertNotIn("本地清理失败", result["notification"])
                self.assertNotIn("敏感中文标题", result["notification"])
                for name in ("video", "cover", "thumb", "preview"):
                    self.assertTrue(getattr(media, name).is_file())
                self.assertTrue((media.video.parent / "telegram-parts-test/part(1).mp4").is_file())
                task = TaskFiles.load(root, Path(result["retained_dir"]), retry=True)
                self.assertNotIn("signature=private", (task.directory / MANIFEST).read_text(encoding="utf-8"))
                self.assertEqual(task.restore_media().video, media.video)

    async def test_notification_failure_does_not_keep_fully_uploaded_media(self):
        with tempfile.TemporaryDirectory() as temp:
            code, result, media = await self.attempt(Path(temp), notify_failure=True)
            self.assertEqual(code, 0)
            self.assertFalse(result["saved_message"])
            self.assertTrue(result["cleaned"])
            self.assertFalse(media.video.exists())

    async def test_retry_reuses_media_and_only_uploads_failed_service(self):
        for failed in ("quark", "telegram"):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _, result, media = await self.attempt(root, tg_success=failed != "telegram", quark_success=failed != "quark")
                tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(return_value=[20, 21]), notify=AsyncMock(), close=AsyncMock())
                q = SimpleNamespace(login=MagicMock(), upload=MagicMock(return_value="quark-retried"), close=MagicMock())
                code, tg_factory, q_factory, prepare = await self.command(
                    root, ["--retry", result["retained_dir"]], tg=tg if failed == "telegram" else None,
                    q=q if failed == "quark" else None,
                )
                self.assertEqual(code, 0)
                prepare.assert_not_awaited()
                if failed == "quark":
                    tg_factory.assert_not_called()
                    self.assertEqual(q.upload.call_args.args[0].video, media.video)
                    q.upload.assert_called_once()
                    self.assertTrue(q.upload.call_args.kwargs["retry"])
                    self.assertEqual(q.upload.call_args.kwargs["checkpoint"]["record_id"], "retained-record")
                else:
                    q_factory.assert_not_called()
                    self.assertEqual(tg.upload.call_args.args[0].video, media.video)
                    tg.upload.assert_awaited_once()
                retried = self.capture.result("123")
                self.assertTrue(retried["cleaned"])
                self.assertEqual(retried["telegram"], "success")
                self.assertEqual(retried["quark"], "success")
                self.assertFalse(media.video.exists())
                self.assertIsNotNone(self.capture.row("123"))

    async def test_retry_drives_official_resume_and_cleans_only_after_both_success(self):
        from test_quark_upload import FakeCLI, result_line
        for expired in (False, True):
            with self.subTest(expired=expired), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _, result, media = await self.attempt(root, quark_success=False)
                record = {"recordId": "retained-record", "fileName": media.video.name,
                          "fileSize": media.video.stat().st_size, "state": "failed", "createdAt": 1}
                listed = [{"code": 0, "type": "list", "data": record},
                          result_line(action="upload-list", data={"totalCount": 1})]
                resumed = ([result_line(-208, "upload-resume", msg="task expired")] if expired else [
                    {"code": 0, "type": "list", "action": "upload-resume",
                     "data": {**record, "fileId": "resumed-file"}},
                    result_line(action="upload-resume", data={"recordId": "retained-record"})])
                cli = FakeCLI([listed, resumed])
                q = quark.Quark(cli=cli)
                q.login = MagicMock()
                code, tg_factory, _, prepare = await self.command(root, ["--retry", result["retained_dir"]], q=q)
                self.assertEqual(code, int(expired))
                tg_factory.assert_not_called()
                prepare.assert_not_awaited()
                self.assertEqual(cli.calls, [["upload", "list"],
                    ["upload", "resume", "--record-id", "retained-record", "--file-path", str(media.video)]])
                final = self.capture.result("123")
                self.assertEqual(final["telegram"], "success")
                self.assertEqual(final["quark"], "failed" if expired else "success")
                self.assertEqual(final["retained"], expired)
                self.assertEqual(media.video.exists(), expired)
                self.assertIsNotNone(self.capture.row("123"))

    async def test_user_delete_removes_only_selected_task_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, _ = await self.attempt(root, quark_success=False)
            other = root / "downloads/other-task"
            other.mkdir()
            (other / "keep.bin").write_bytes(b"keep")
            session = root / ".state/telegram.session"
            session.parent.mkdir(exist_ok=True)
            session.write_bytes(b"session-marker")
            code, tg_factory, q_factory, prepare = await self.command(root, ["--delete", result["retained_dir"]])
            self.assertEqual(code, 0)
            self.assertFalse(Path(result["retained_dir"]).exists())
            self.assertEqual((other / "keep.bin").read_bytes(), b"keep")
            self.assertEqual(session.read_bytes(), b"session-marker")
            self.assertIsNotNone(self.capture.row("123"))
            tg_factory.assert_not_called()
            q_factory.assert_not_called()
            prepare.assert_not_awaited()
            self.assertTrue(self.capture.result("123", event="cleanup_result")["cleaned"])

    async def test_outside_task_directory_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "keep.bin"
            marker.write_bytes(b"keep")
            code, _, _, _ = await self.command(root, ["--delete", str(root)])
            self.assertEqual(code, 1)
            self.assertEqual(marker.read_bytes(), b"keep")

    async def test_reparse_directory_is_rejected_without_following_it(self):
        if not hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
            self.skipTest("仅用于 Windows 重解析目录保护")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, media = await self.attempt(root, quark_success=False)
            directory = Path(result["retained_dir"])
            real_lstat = Path.lstat
            mode = real_lstat(directory).st_mode
            def linked(path, *args, **kwargs):
                if path == directory:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return real_lstat(path, *args, **kwargs)
            with patch.object(Path, "lstat", linked):
                with self.assertRaises(JobError):
                    TaskFiles.load(root, directory)
            self.assertTrue(media.video.exists())

    async def test_deleting_old_task_keeps_latest_retained_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, first, first_media = await self.attempt(root, quark_success=False)
            _, latest, latest_media = await self.attempt(root, quark_success=False)
            code, _, _, _ = await self.command(root, ["--delete", first["retained_dir"]])
            self.assertEqual(code, 0)
            self.assertFalse(first_media.video.exists())
            self.assertTrue(latest_media.video.exists())
            current = self.capture.result("123")
            self.assertEqual(current["retained_dir"], latest["retained_dir"])
            self.assertTrue(current["awaiting_decision"])

    async def test_manifest_cannot_reference_media_outside_its_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, _ = await self.attempt(root, quark_success=False)
            outside = root / "outside.mp4"
            outside.write_bytes(b"private")
            manifest = Path(result["retained_dir"]) / MANIFEST
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["media"]["files"]["video"]["path"] = "../../outside.mp4"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            task = TaskFiles.load(root, manifest.parent, retry=True)
            with self.assertRaises(JobError):
                task.restore_media()
            self.assertEqual(outside.read_bytes(), b"private")

    async def test_interrupted_task_requires_remote_review_before_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, _ = await self.attempt(root, quark_success=False)
            manifest = Path(result["retained_dir"]) / MANIFEST
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["state"] = "running"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(JobError, "核查"):
                TaskFiles.load(root, manifest.parent, retry=True)

    async def test_partial_cleanup_failure_restores_task_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, _ = await self.attempt(root, quark_success=False)
            task = TaskFiles.load(root, Path(result["retained_dir"]))
            def fail_delete(target):
                (target / MANIFEST).unlink()
                raise PermissionError("busy file")
            with patch("task_files.shutil.rmtree", side_effect=fail_delete):
                with self.assertRaises(PermissionError):
                    task.delete(root)
            self.assertTrue((task.directory / MANIFEST).exists())
            self.assertEqual(TaskFiles.load(root, task.directory).data["id"], "123")

    async def test_changed_media_is_not_reuploaded_or_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, media = await self.attempt(root, quark_success=False)
            media.video.write_bytes(b"changed")
            q = SimpleNamespace(login=MagicMock(), upload=MagicMock(return_value="unexpected"), close=MagicMock())
            code, _, _, _ = await self.command(root, ["--retry", result["retained_dir"]], q=q)
            self.assertEqual(code, 1)
            q.upload.assert_not_called()
            self.assertEqual(media.video.read_bytes(), b"changed")

    async def test_active_task_lock_blocks_delete(self):
        from filelock import FileLock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, result, media = await self.attempt(root, quark_success=False)
            with FileLock(str(root / ".state/task.lock"), timeout=0):
                code, _, _, _ = await self.command(root, ["--delete", result["retained_dir"]])
            self.assertEqual(code, 1)
            self.assertTrue(media.video.exists())

    async def test_prepare_failure_keeps_downloaded_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seen = []
            async def fail_prepare(_, directory, _config, **kwargs):
                video = directory / "already-downloaded.mp4"
                video.write_bytes(b"downloaded")
                seen.append(video)
                raise JobError("预处理失败")
            tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(), notify=AsyncMock(), close=AsyncMock())
            q = SimpleNamespace(login=MagicMock(), upload=MagicMock(), close=MagicMock())
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(source, "prepare_media", side_effect=fail_prepare), \
                    patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                    redirect_stdout(StringIO()):
                self.assertEqual(await main.operate(CONFIG, video_id="123"), 1)
            self.assertTrue(seen[0].exists())
            result = self.capture.result("123")
            self.assertTrue(result["awaiting_decision"])
            tg.upload.assert_not_awaited()
            q.upload.assert_not_called()

    async def test_failed_cleanup_keeps_recoverable_task_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(TaskFiles, "delete", side_effect=PermissionError("busy file")):
                _, result, _ = await self.attempt(root)
            self.assertTrue(result["retained"])
            self.assertIn("本地清理失败", result["notification"])
            task = TaskFiles.load(root, Path(result["retained_dir"]), retry=True)
            self.assertEqual(task.data["state"], "complete")
            code, tg_factory, q_factory, prepare = await self.command(root, ["--retry", result["retained_dir"]])
            self.assertEqual(code, 0)
            tg_factory.assert_not_called()
            q_factory.assert_not_called()
            prepare.assert_not_awaited()
            self.assertFalse(task.directory.exists())

    async def test_retained_split_directory_survives_upload_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "original.mp4"
            video.write_bytes(b"x" * 400)
            media = SimpleNamespace(video=video, video_meta={"duration": 10})
            async def remux(*args):
                pattern = str(args[-1])
                Path(pattern.replace("%06d", "000001")).write_bytes(b"x" * 50)
                Path(pattern.replace("%06d", "000002")).write_bytes(b"x" * 50)
            paths = []
            with patch.object(telegram_media, "split_points", new=AsyncMock(return_value=[1.0])), \
                    patch.object(telegram_media, "ffmpeg", side_effect=remux), \
                    patch.object(telegram_media, "media_metadata", return_value={"duration": 5}), \
                    redirect_stdout(StringIO()):
                with self.assertRaises(JobError):
                    async with telegram_media.telegram_videos(media, max_bytes=100, retain=True) as parts:
                        paths = [path for path, _ in parts]
                        raise JobError("上传失败")
            self.assertTrue(paths)
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(video.read_bytes(), b"x" * 400)


if __name__ == "__main__":
    unittest.main()
