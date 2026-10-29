"""官方夸克 CLI 适配层的离线回归：不执行 Node、不授权、不上传真实文件。"""

from contextlib import chdir, redirect_stderr, redirect_stdout
import hashlib
import json
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from common import JobError
import quark
import quark_upload_test as console


def result_line(code=0, action="upload", data=None, msg="成功"):
    return {"code": code, "msg": msg, "action": action, "type": "result", "data": data or {}}


class FakeCLI:
    """按调用顺序返回预设的标准输出行；参数与真实 QuarkCLI 一致。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _respond(self, args, on_line):
        self.calls.append(list(args))
        response = self.responses.pop(0) if self.responses else [result_line()]
        if isinstance(response, BaseException):
            raise response
        lines = response() if callable(response) else response
        for line in lines:
            if on_line is not None:
                on_line(line)
        return 0, list(lines), ""

    def run(self, args, on_line=None, **_):
        return self._respond(args, on_line)


class RuntimeTests(unittest.TestCase):
    def test_vendored_runtime_is_pinned_and_present(self):
        info = quark.verify_runtime()
        self.assertEqual(info["quark_cli_version"], "1.0.20")
        self.assertTrue(info["node"].startswith("v"))
        for name, digest in quark.CHECKSUMS.items():
            self.assertEqual(hashlib.sha256((quark.VENDOR / name).read_bytes()).hexdigest(), digest)

    def test_runtime_runs_outside_skill_package_so_authorization_is_not_distributed(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / ".state" / "quark-cli" / "scripts"
            with patch.object(quark, "RUNTIME", runtime):
                quark.prepare_runtime()
                self.assertEqual(sorted(path.name for path in runtime.iterdir()),
                                 sorted([*quark.CHECKSUMS, quark.PROXY_NAME]))
                self.assertFalse((runtime.parent / "claudecode").exists())
                # 官方 CLI 会把 host 目录放在脚本目录的上两级，必须落在 .state 内部。
                self.assertEqual(runtime.parent.parent.name, ".state")
                copied = (runtime / "quark-drive.cjs").read_bytes()
                (runtime / "quark-drive.cjs").write_bytes(b"old")
                quark.prepare_runtime()
                self.assertEqual((runtime / "quark-drive.cjs").read_bytes(), copied)

    def test_modified_or_missing_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            vendor = Path(temp)
            for name in quark.CHECKSUMS:
                (vendor / name).write_bytes(b"tampered")
            with patch.object(quark, "VENDOR", vendor), self.assertRaisesRegex(JobError, "校验失败"):
                quark.verify_runtime()
            (vendor / "quark-drive.cjs").unlink()
            with patch.object(quark, "VENDOR", vendor), self.assertRaisesRegex(JobError, "缺少官方"):
                quark.verify_runtime()

    def test_missing_node_is_reported_before_any_upload(self):
        with patch.object(quark.shutil, "which", return_value=None), \
                patch.dict(os.environ, {"QUARK_NODE": ""}, clear=False):
            os.environ.pop("QUARK_NODE", None)
            with self.assertRaisesRegex(JobError, "Node.js"):
                quark.verify_runtime()

    def test_old_node_is_rejected(self):
        completed = MagicMock(returncode=0, stdout="v14.21.3\n")
        with patch.object(quark.shutil, "which", return_value="node"), \
                patch.object(quark.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(JobError, "16 或更高"):
                quark.verify_runtime()

    def test_cli_is_invoked_with_claude_code_identity_and_pinned_version_env(self):
        with patch.object(quark, "prepare_runtime"):
            cli = quark.QuarkCLI(node="node", script=quark.cli_path())
        self.assertEqual(cli.env["CLAUDECODE"], "1")
        self.assertNotIn("QUARK_CONFIG_DIR", cli.env)
        self.assertRegex(cli.session_id, r"^\d{10}-[a-z0-9]{6}$")
        self.assertEqual(cli._argv(["get-user-info"])[-2:], ["--session-id", cli.session_id])
        self.assertEqual(cli.script.parent.parent, quark.STATE / "quark-cli")


class LoginTests(unittest.TestCase):
    def test_saved_authorization_is_reused_without_starting_browser(self):
        cli = FakeCLI([[result_line(action="get-user-info", data={"nickname": "test"})]])
        quark.Quark(cli=cli).login()
        self.assertEqual(cli.calls, [["get-user-info"]])

    def test_missing_authorization_starts_official_login_once(self):
        cli = FakeCLI([[result_line(-103, "login")], [result_line(0, "login")]])
        quark.Quark(cli=cli).login(prompt=MagicMock())
        self.assertEqual(cli.calls, [["get-user-info"], ["login"]])

    def test_authorization_failure_uses_pasted_code_once(self):
        prompt = MagicMock(return_value="abc123-code")
        cli = FakeCLI([[result_line(-103, "login")], [result_line(-1201, "login", msg="浏览器打开失败")],
                       [result_line(0, "login")]])
        quark.Quark(cli=cli).login(prompt=prompt)
        self.assertEqual(cli.calls[2], ["login", "--token", "abc123-code"])
        self.assertEqual(prompt.call_count, 1)

    def test_existing_official_authorization_is_not_treated_as_failure(self):
        cli = FakeCLI([[result_line(0, "login", msg="已授权")]])
        quark.Quark(cli=cli).login(force=True, prompt=MagicMock())
        self.assertEqual(cli.calls, [["login"]])

    def test_empty_or_unsafe_code_cancels_without_calling_cli(self):
        for value in ("", "  ", "has space", "x" * 600, "a;rm -rf"):
            with self.subTest(value=value), self.assertRaises(quark.AuthRequired):
                cli = FakeCLI([[result_line(-103, "login")], [result_line(-1201, "login", msg="浏览器打开失败")]])
                quark.Quark(cli=cli).login(prompt=MagicMock(return_value=value))
            self.assertNotIn("--token", [item for call in cli.calls for item in call])

    def test_missing_prompt_still_reports_authorization_required(self):
        cli = FakeCLI([[result_line(-103, "login")], [result_line(-1201, "login", msg="浏览器打开失败")]])
        with self.assertRaises(quark.AuthRequired):
            quark.Quark(cli=cli).login()

    def test_forced_login_skips_reuse(self):
        cli = FakeCLI([[result_line(0, "login")]])
        quark.Quark(cli=cli).login(force=True)
        self.assertEqual(cli.calls, [["login"]])

    def test_non_auth_failure_is_not_mistaken_for_missing_authorization(self):
        cli = FakeCLI([[result_line(-5001, "get-user-info", msg="网络异常")]])
        with self.assertRaises(JobError) as caught:
            quark.Quark(cli=cli).login(prompt=MagicMock())
        self.assertNotIsInstance(caught.exception, quark.AuthRequired)
        self.assertEqual(cli.calls, [["get-user-info"]])


class FolderTests(unittest.TestCase):
    def test_each_segment_is_created_under_the_previous_fid(self):
        cli = FakeCLI([[result_line(0, "create-folder", {"fid": "root-child"})],
                       [result_line(0, "create-folder", {"fid": "nested"})]])
        self.assertEqual(quark.Quark(cli=cli).folder("/JAVHD/资料"), "nested")
        self.assertEqual(cli.calls, [["create-folder", "--dir-path", "JAVHD", "--parent-fid", "0"],
                                     ["create-folder", "--dir-path", "资料", "--parent-fid", "root-child"]])

    def test_root_and_unsafe_paths(self):
        cli = FakeCLI([])
        self.assertEqual(quark.Quark(cli=cli).folder("/"), "0")
        self.assertEqual(cli.calls, [])
        for path in ("/a/../b", "/a/./b"):
            with self.subTest(path=path), self.assertRaises(JobError):
                quark.Quark(cli=cli).folder(path)
        self.assertEqual(cli.calls, [])

    def test_missing_fid_is_not_treated_as_root(self):
        cli = FakeCLI([[result_line(0, "create-folder", {})]])
        with self.assertRaisesRegex(JobError, "目录 ID"):
            quark.Quark(cli=cli).folder("/JAVHD")


class UploadPathTests(unittest.TestCase):
    def test_relative_file_is_not_rebased_against_cli_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cli_cwd = root / ".state" / "quark-cli"
            cli_cwd.mkdir(parents=True)
            source = root / ".state" / "quark-cli-test.txt"
            source.write_bytes(b"x" * 50)
            progress = []
            def response():
                supplied = Path(cli.calls[-1][1])
                self.assertTrue(supplied.is_absolute())
                self.assertEqual((cli_cwd / supplied).resolve(), source)
                self.assertEqual(supplied.read_bytes(), b"x" * 50)
                return [{"type": "progress", "data": {"current": 50, "total": 50}},
                        result_line(data={"fids": ["file-id"]})]
            cli = FakeCLI([response])
            with chdir(root):
                fid = quark.Quark(cli=cli).upload_file(Path(".state") / source.name,
                    parent_folder_id="target", transfer_callback=lambda *args: progress.append(args))
            self.assertEqual(fid, "file-id")
            self.assertEqual(cli.calls[0], ["upload", str(source), "--parent-fid", "target"])
            self.assertEqual(progress, [(50, 50)])

    def test_home_directory_is_expanded_before_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            source = home / "tiny.txt"
            source.write_bytes(b"x" * 50)
            cli = FakeCLI([[result_line(data={"fids": ["file-id"]})]])
            with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                self.assertEqual(quark.Quark(cli=cli).upload_file("~/tiny.txt"), "file-id")
            self.assertEqual(cli.calls[0][1], str(source))

    def test_invalid_files_report_absolute_path_without_starting_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "empty.txt").touch()
            (root / "directory").mkdir()
            for name, message in (("missing.txt", "不存在"), ("empty.txt", "为空"),
                                  ("directory", "不是文件")):
                with self.subTest(name=name), chdir(root):
                    cli = FakeCLI([])
                    with self.assertRaises(JobError) as caught:
                        quark.Quark(cli=cli).upload_file(name)
                    self.assertIn(message, str(caught.exception))
                    self.assertIn(str(root / name), str(caught.exception))
                    self.assertEqual(cli.calls, [])

    def test_media_upload_delegates_relative_path_without_changing_javhd_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "video.mp4"
            source.write_bytes(b"x" * 50)
            cli = FakeCLI([[result_line(action="create-folder", data={"fid": "javhd"})],
                           [result_line(data={"fids": ["file-id"]})]])
            with chdir(root):
                self.assertEqual(quark.Quark(cli=cli).upload(SimpleNamespace(video=Path(source.name))), "file-id")
            self.assertEqual(cli.calls[0], ["create-folder", "--dir-path", "JAVHD", "--parent-fid", "0"])
            self.assertEqual(cli.calls[1], ["upload", str(source), "--parent-fid", "javhd"])

    def test_unchanged_check_keeps_absolute_path_if_cwd_changes_during_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "tiny.txt"
            source.write_bytes(b"x" * 50)
            other = root / "other-cwd"
            other.mkdir()
            def response():
                os.chdir(other)
                return [result_line(data={"fids": ["file-id"]})]
            cli = FakeCLI([response])
            with chdir(root):
                self.assertEqual(quark.Quark(cli=cli).upload_file(source.name), "file-id")
            self.assertEqual(cli.calls[0][1], str(source))
            self.assertEqual(source.read_bytes(), b"x" * 50)


class UploadTests(unittest.TestCase):
    def upload(self, cli, *, size=6 * 1024 * 1024, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "资料.bin"
            path.write_bytes(b"a" * size)
            kwargs.setdefault("parent_folder_id", "target")
            fid = quark.Quark(cli=cli).upload_file(path, **kwargs)
            self.assertEqual(path.read_bytes(), b"a" * size)
        return fid

    def test_final_result_and_file_id_are_required(self):
        cli = FakeCLI([[result_line(0, "upload", {"fids": ["fid-1"], "successCount": 1})]])
        self.assertEqual(self.upload(cli), "fid-1")
        self.assertEqual(cli.calls[0][0], "upload")
        self.assertEqual(cli.calls[0][2:], ["--parent-fid", "target"])
        self.assertTrue(cli.calls[0][1].endswith("资料.bin"))

    def test_progress_and_instant_flag_come_from_official_lines(self):
        progress = []
        lines = [
            {"type": "progress", "data": {"current": 4 * 1024 * 1024, "total": 6 * 1024 * 1024, "percent": 66}},
            {"code": 0, "msg": "上传成功", "action": "upload", "type": "list",
             "data": {"fileId": "fid-instant", "instantUpload": True, "fileName": "资料.bin"}},
            {"code": 0, "msg": "成功", "action": "upload", "type": "result",
             "data": {"fids": ["fid-instant"]}},
        ]
        stages = []
        self.assertEqual(self.upload(FakeCLI([lines]), progress_callback=lambda *args: stages.append(args),
                                    transfer_callback=lambda *args: progress.append(args)), "fid-instant")
        self.assertEqual(progress, [(4 * 1024 * 1024, 6 * 1024 * 1024)])
        self.assertEqual(stages[-1], (100, "秒传完成，正在核验远端文件..."))

    def test_missing_file_id_is_unconfirmed_not_success(self):
        lines = [{"code": 31003, "msg": "file hash conflict", "action": "upload", "type": "list",
                  "data": {"recordId": "rec"}},
                 result_line(-204, "upload", msg="上传操作失败, 失败任务数量: 1")]
        with self.assertRaises(quark.UploadUnconfirmed) as caught:
            self.upload(FakeCLI([lines]))
        self.assertIn("31003", str(caught.exception))
        self.assertIn("file hash conflict", str(caught.exception))

    def test_success_result_without_fids_is_unconfirmed(self):
        with self.assertRaises(quark.UploadUnconfirmed):
            self.upload(FakeCLI([[result_line(0, "upload", {"fids": [], "successCount": 1})]]))

    def test_cli_error_never_reports_a_file_id(self):
        cli = FakeCLI([[result_line(-202, "upload", msg="文件不存在")]])
        with self.assertRaises(quark.UploadUnconfirmed):
            self.upload(cli)

    def test_authentication_errors_are_not_downgraded_to_unconfirmed(self):
        cli = FakeCLI([[result_line(-103, "login")]])
        with self.assertRaises(quark.AuthRequired):
            self.upload(cli)

    def test_local_file_changed_during_upload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "changing.bin"
            path.write_bytes(b"a" * 10)
            cli = FakeCLI([[result_line(0, "upload", {"fids": ["fid"]})]])

            def mutate(args, on_line):
                path.write_bytes(b"changed!")
                on_line(result_line(0, "upload", {"fids": ["fid"]}))
                return 0, [], ""

            cli.run = mutate
            with self.assertRaisesRegex(JobError, "发生变化"):
                quark.Quark(cli=cli).upload_file(path)

    def test_empty_file_is_rejected_before_cli(self):
        cli = FakeCLI([])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.bin"
            path.touch()
            with self.assertRaises(JobError):
                quark.Quark(cli=cli).upload_file(path)
        self.assertEqual(cli.calls, [])

    def test_error_text_strips_credentials(self):
        payload = {"code": -1201, "msg": "Bearer abc token=refresh_token secret"}
        self.assertIn("已省略", quark.error_text(payload))
        self.assertNotIn("secret", quark.error_text(payload))
        self.assertEqual(quark.error_text({"code": -202, "msg": "文件不存在: /x"}).count("\n"), 0)


class ConsoleTests(unittest.TestCase):
    def test_progress_uses_bytes_and_reports_speed(self):
        now = [100.0]
        output = StringIO()
        progress = console.ConsoleProgress(1024 * 1024, stream=output, clock=lambda: now[0])
        progress.transfer(0, 1024 * 1024)
        now[0] += 1
        progress.transfer(512 * 1024, 1024 * 1024)
        progress.render()
        self.assertIn("50.00%", output.getvalue())
        self.assertIn("512.00 KiB/s", output.getvalue())

    def test_instant_upload_is_not_shown_as_zero_percent(self):
        output = StringIO()
        progress = console.ConsoleProgress(1000, stream=output)
        progress.stage(100, "秒传完成，正在核验远端文件...")
        progress.render()
        self.assertIn("秒传完成，无需发送文件数据", output.getvalue())
        self.assertNotIn("0.00%", output.getvalue())
        self.assertTrue(progress.instant)

    def test_interactive_entry_uploads_selected_file(self):
        remote = MagicMock()
        remote.folder.return_value = "selected-folder"
        remote.upload_file.return_value = "uploaded-id"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "测试文件.bin"
            path.write_bytes(b"keep-local")
            with patch.object(console, "Quark", return_value=remote), \
                    patch("builtins.input", side_effect=[f'"{path}"', "/JAVHD"]), \
                    redirect_stdout(StringIO()) as out:
                console.QuarkUploadTest().run()
            remote.login.assert_called_once()
            self.assertTrue(callable(remote.login.call_args.kwargs["prompt"]))
            remote.folder.assert_called_once_with("/JAVHD")
            self.assertEqual(remote.upload_file.call_args.args, (path.resolve(),))
            self.assertIn("上传成功", out.getvalue())
            self.assertEqual(path.read_bytes(), b"keep-local")

    def test_empty_missing_and_directory_inputs_are_reprompted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "valid.bin"
            path.write_bytes(b"test")
            empty = Path(temp) / "empty.bin"
            empty.touch()
            with patch("builtins.input", side_effect=["", str(path / "missing"), temp, str(empty), str(path)]), \
                    redirect_stdout(StringIO()):
                self.assertEqual(console.QuarkUploadTest.read_file(), path.resolve())

    def test_failure_message_does_not_leak_unknown_exception_text(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(console, "STATE", Path(temp)), \
                patch.object(console, "prepare_runtime"), patch.object(console, "QuarkUploadTest") as runner, \
                redirect_stderr(StringIO()) as out:
            runner.return_value.run.side_effect = RuntimeError("secret-token-value")
            self.assertEqual(console.main([]), 1)
        self.assertIn("RuntimeError", out.getvalue())
        self.assertNotIn("secret-token-value", out.getvalue())

    def test_unconfirmed_outcome_is_not_reported_as_definite_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(console, "STATE", Path(temp)), \
                patch.object(console, "prepare_runtime"), patch.object(console, "QuarkUploadTest") as runner, \
                redirect_stderr(StringIO()) as out:
            runner.return_value.run.side_effect = quark.UploadUnconfirmed("官方未返回文件 ID")
            self.assertEqual(console.main([]), 1)
        self.assertIn("上传结果未确认", out.getvalue())
        self.assertNotIn("测试失败", out.getvalue())

    def test_help_does_not_execute_node(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            completed = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tests/quark_upload_test.py"), "--help"],
                cwd=temp, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--relogin", completed.stdout)


if __name__ == "__main__":
    unittest.main()
