"""夸克断点持久化、跨实例恢复、保守匹配和取消保存协议；不访问真实账号。"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from common import JobError
import quark
from task_files import MANIFEST, TaskFiles
from test_quark_upload import FakeCLI, result_line


def listing(records=()):
    return [{"code": 0, "type": "list", "action": "upload-list", "data": record} for record in records] + [
        result_line(action="upload-list", data={"totalCount": len(records)})]


def record(path, record_id="rec-1", state="failed", created=None):
    return {"recordId": record_id, "fileName": path.name, "fileSize": path.stat().st_size,
            "state": state, "createdAt": created or int(time.time() * 1000)}


def checkpoint(path, **updates):
    data = {"version": 1, "attempted": True, "file_name": path.name, "file_size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns, "parent_fid": "target", "record_id": "rec-1",
            "previous_ids": [], "session_id": "1234567890-abcdef", "started_at": int(time.time() * 1000)}
    return {**data, **updates}


def completed(path, record_id="rec-1"):
    return [{"type": "progress", "action": "upload-resume", "data": {"current": 7, "total": 10}},
            {"code": 0, "type": "list", "action": "upload-resume", "data": {
                "recordId": record_id, "fileName": path.name, "fileSize": path.stat().st_size,
                "fileId": "remote-file", "instantUpload": False}},
            result_line(action="upload-resume", data={"recordId": record_id, "fileName": path.name})]


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "中文 video.mp4"
        self.path.write_bytes(b"video-data")
        self.saved = []

    def save(self, data):
        quark.validate_checkpoint(data)
        self.saved.append(deepcopy(data))

    def resume(self, responses, data=None):
        cli = FakeCLI(responses)
        value = quark.Quark(cli=cli).upload_resumable(
            self.path, checkpoint=checkpoint(self.path) if data is None else data,
            checkpoint_callback=self.save, retry=True)
        return value, cli

    def test_failure_saves_record_before_result_then_new_instance_resumes_same_session(self):
        failure = [{"code": -204, "type": "list", "data": {"recordId": "rec-1"}},
                   result_line(-204, msg="temporary failure")]
        cli = FakeCLI([[result_line(action="create-folder", data={"fid": "target"})], listing(), failure])
        cli.session_id = "1234567890-abcdef"
        with self.assertRaises(quark.UploadUnconfirmed):
            quark.Quark(cli=cli).upload_resumable(self.path, checkpoint_callback=self.save)
        self.assertTrue(self.saved[0]["attempted"])
        self.assertIsNone(self.saved[0]["record_id"])
        self.assertEqual(self.saved[1]["record_id"], "rec-1")
        # 模拟进程重启：从 JSON 读取，而不是复用旧 Quark / CLI 对象。
        state = json.loads(json.dumps(self.saved[-1]))
        value, resumed = self.resume([listing([record(self.path)]), completed(self.path)], state)
        self.assertEqual(value, "remote-file")
        self.assertEqual(resumed.session_id, cli.session_id)
        self.assertEqual(resumed.calls, [["upload", "list"], ["upload", "resume", "--record-id", "rec-1",
                                                              "--file-path", str(self.path.resolve())]])

    def test_failure_without_terminal_line_discovers_only_new_matching_record(self):
        old = record(self.path, "old", created=int(time.time() * 1000) - 100000)
        cli = FakeCLI([[result_line(action="create-folder", data={"fid": "target"})], listing([old]),
                       JobError("命令超时"), listing([old, record(self.path)])])
        with self.assertRaisesRegex(quark.UploadUnconfirmed, "命令超时"):
            quark.Quark(cli=cli).upload_resumable(self.path, checkpoint_callback=self.save)
        self.assertEqual(self.saved[-1]["record_id"], "rec-1")
        self.assertEqual(self.saved[-1]["previous_ids"], ["old"])

    def test_retry_can_discover_checkpoint_after_failure_to_query_records(self):
        data = checkpoint(self.path, record_id=None)
        value, cli = self.resume([listing([record(self.path)]), completed(self.path)], data)
        self.assertEqual(value, "remote-file")
        self.assertEqual(self.saved[-1]["record_id"], "rec-1")
        self.assertEqual(cli.calls[-1][1], "resume")

    def test_missing_ambiguous_old_or_unrelated_records_never_start_fresh_upload(self):
        for records in ([], [record(self.path, "a"), record(self.path, "b")],
                        [record(self.path, created=1)],
                        [{**record(self.path), "fileName": "unrelated.mp4"}],
                        [{**record(self.path), "fileSize": 12345}]):
            with self.subTest(records=records):
                cli = FakeCLI([listing(records)])
                with self.assertRaisesRegex(quark.UploadUnconfirmed, "唯一确认"):
                    quark.Quark(cli=cli).upload_resumable(
                        self.path, checkpoint=checkpoint(self.path, record_id=None),
                        checkpoint_callback=self.save, retry=True)
                self.assertEqual(cli.calls, [["upload", "list"]])

    def test_known_record_must_exist_match_file_and_not_already_be_successful(self):
        for records in ([], [{**record(self.path), "fileSize": 999}], [record(self.path, state="success")],
                        [record(self.path, state="unexpected")]):
            with self.subTest(records=records):
                cli = FakeCLI([listing(records)])
                with self.assertRaises(quark.UploadUnconfirmed):
                    quark.Quark(cli=cli).upload_resumable(self.path, checkpoint=checkpoint(self.path),
                                                         checkpoint_callback=self.save, retry=True)
                self.assertEqual(cli.calls, [["upload", "list"]])

    def test_expired_or_missing_restore_record_never_falls_back(self):
        for code in (-205, -208, -204):
            with self.subTest(code=code):
                cli = FakeCLI([listing([record(self.path)]), [result_line(code, "upload-resume", msg="task expired")]])
                with self.assertRaisesRegex(quark.UploadUnconfirmed, "不会自动重新上传"):
                    quark.Quark(cli=cli).upload_resumable(self.path, checkpoint=checkpoint(self.path),
                                                         checkpoint_callback=self.save, retry=True)
                self.assertEqual(len(cli.calls), 2)
                self.assertEqual(cli.calls[-1][1], "resume")

    def test_legacy_missing_checkpoint_is_refused_before_cli(self):
        cli = FakeCLI([])
        with self.assertRaisesRegex(JobError, "旧任务没有"):
            quark.Quark(cli=cli).upload_resumable(self.path, checkpoint_callback=self.save, retry=True)
        self.assertEqual(cli.calls, [])

    def test_not_yet_attempted_task_may_start_its_first_upload_on_retry(self):
        cli = FakeCLI([[result_line(action="create-folder", data={"fid": "target"})], listing(),
                       [result_line(data={"fids": ["fresh-file"]})]])
        value = quark.Quark(cli=cli).upload_resumable(self.path, checkpoint={"version": 1, "attempted": False},
                                                    checkpoint_callback=self.save, retry=True)
        self.assertEqual(value, "fresh-file")
        self.assertEqual(cli.calls[-1], ["upload", str(self.path.resolve()), "--parent-fid", "target"])

    def test_checkpoint_must_be_durable_before_new_upload_starts(self):
        cli = FakeCLI([[result_line(action="create-folder", data={"fid": "target"})], listing()])
        def fail_save(_data):
            raise OSError("disk full")
        with self.assertRaises(OSError):
            quark.Quark(cli=cli).upload_resumable(self.path, checkpoint_callback=fail_save)
        self.assertEqual(len(cli.calls), 2)

    def test_changed_file_fingerprint_prevents_even_resume_command(self):
        data = checkpoint(self.path)
        self.path.write_bytes(b"changed")
        cli = FakeCLI([])
        with self.assertRaisesRegex(JobError, "发生变化"):
            quark.Quark(cli=cli).upload_resumable(self.path, checkpoint=data, checkpoint_callback=self.save, retry=True)
        self.assertEqual(cli.calls, [])

    def test_resume_requires_matching_success_list_and_final_result(self):
        original = completed(self.path)
        bad_cases = [original[:1] + original[-1:], original[:-1],
                     [original[0], original[1], result_line(action="upload-resume", data={"recordId": "wrong"})],
                     [original[0], {**original[1], "data": {**original[1]["data"], "recordId": "wrong"}}, original[-1]],
                     [original[0], {**original[1], "data": {**original[1]["data"], "fileSize": 999}}, original[-1]],
                     original + [result_line(-204, "upload-resume")]]
        for lines in bad_cases:
            with self.subTest(lines=lines), self.assertRaises(quark.UploadUnconfirmed):
                quark.Quark(cli=FakeCLI([lines])).resume_file(self.path, "rec-1")

    def test_file_change_while_listing_records_is_rejected_before_resume(self):
        data = checkpoint(self.path)
        def changed_listing():
            lines = listing([record(self.path)])
            self.path.write_bytes(b"changed during task lookup")
            return lines
        cli = FakeCLI([changed_listing])
        with self.assertRaisesRegex(JobError, "发生变化"):
            quark.Quark(cli=cli).upload_resumable(self.path, checkpoint=data,
                                                 checkpoint_callback=self.save, retry=True)
        self.assertEqual(cli.calls, [["upload", "list"]])

    def test_resume_reports_official_progress_and_preserves_proxy_and_auth_errors(self):
        progress = []
        self.assertEqual(quark.Quark(cli=FakeCLI([completed(self.path)])).resume_file(
            self.path, "rec-1", transfer_callback=lambda *values: progress.append(values)), "remote-file")
        self.assertEqual(progress, [(7, 10)])
        for error in (quark.ProxyConnectionError("ETIMEDOUT"), quark.DirectConnectionError("ETIMEDOUT"),
                      quark.AuthRequired("expired")):
            with self.subTest(error=error), self.assertRaises(type(error)):
                quark.Quark(cli=FakeCLI([error])).resume_file(self.path, "rec-1")

    def test_file_changed_during_resume_never_counts_as_success(self):
        def response():
            lines = completed(self.path)
            self.path.write_bytes(b"modified")
            return lines
        with self.assertRaisesRegex(quark.UploadUnconfirmed, "发生变化"):
            quark.Quark(cli=FakeCLI([response])).resume_file(self.path, "rec-1")

    def test_incomplete_or_duplicate_record_listing_is_rejected(self):
        for lines in ([result_line(action="upload-list")],
                      listing([record(self.path), record(self.path)]),
                      [result_line(action="upload-list", data={"totalCount": 1})]):
            with self.subTest(lines=lines), self.assertRaises(JobError):
                quark.Quark(cli=FakeCLI([lines])).upload_records()

    def test_task_checkpoint_and_telegram_status_survive_concurrent_saves(self):
        root = Path(self.temp.name)
        task = TaskFiles.create(root, "123", ["#4k"], -100123)
        task.save({"telegram": "failed", "quark": "failed"}, {}, state="running")
        def checkpoints():
            for number in range(20):
                task.save_quark_checkpoint(checkpoint(self.path, record_id=f"record-{number}"))
        def telegram_results():
            for _ in range(20):
                task.save({"telegram": "success", "quark": "failed"}, {"telegram_remote_ids": [12]}, state="running")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(checkpoints), pool.submit(telegram_results)]
            for future in futures:
                future.result()
        data = json.loads((task.directory / MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(data["quark_upload"]["record_id"], "record-19")
        self.assertEqual(data["statuses"]["telegram"], "success")
        self.assertEqual(data["remote_ids"]["telegram"], [12])


@unittest.skipUnless(shutil.which("node"), "需要 Node.js")
class CancellationTests(unittest.TestCase):
    def test_timeout_invokes_official_sigint_handler_before_force_kill(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "runtime/scripts/fake.cjs"
            script.parent.mkdir(parents=True)
            marker = root / "checkpoint-saved"
            script.write_text("process.on('SIGINT', () => { require('node:fs').writeFileSync(" +
                              json.dumps(str(marker)) + ", 'saved'); process.exit(0); });\n" +
                              "setInterval(() => {}, 1000);\n", encoding="utf-8")
            with patch.object(quark, "RUNTIME", script.parent):
                cli = quark.QuarkCLI(script=script, timeout=0.6)
                with self.assertRaisesRegex(JobError, "命令超时"):
                    cli.run(["upload"])
            self.assertEqual(marker.read_text(), "saved")


if __name__ == "__main__":
    unittest.main()
