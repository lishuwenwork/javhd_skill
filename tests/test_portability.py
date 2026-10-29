"""频道配置、规范分发和跨目录运行；不连接外部服务。"""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common
import package_skill
import telegram

TEST_CONFIG = {
    "telegram_api_id": 0,
    "telegram_api_hash": "",
    "telegram_channel_id": 0,
    "telegram_proxy": "",
    "http_proxy": "",
    "javhd_cookies_file": "",
    "upload_concurrency": 4,
    "notify_saved_messages": True,
}


class ChannelConfigTests(unittest.TestCase):
    def test_loads_config_json_without_template(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {**TEST_CONFIG, "telegram_channel_id": "-1001112223334"}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with patch.object(common, "ROOT", root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(common.load_config()["telegram_channel_id"], -1001112223334)

    def test_missing_config_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(common, "ROOT", Path(temp)), self.assertRaisesRegex(common.JobError, "config.example.json"):
                common.load_config()
            self.assertFalse((Path(temp) / "config.json").exists())

    def test_invalid_config_json_is_reported(self):
        for text in ("{", "[]", '"not-an-object"'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "config.json").write_text(text, encoding="utf-8")
                with patch.object(common, "ROOT", root), self.assertRaises(common.JobError):
                    common.load_config()

    def test_invalid_channel_is_rejected(self):
        for value in ("@somewhere", True, 123, "not-an-id"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = {**TEST_CONFIG, "telegram_channel_id": value}
                (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                with patch.object(common, "ROOT", root), self.assertRaises(common.JobError):
                    common.load_config()

    def test_environment_overrides_config_but_not_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {**TEST_CONFIG, "telegram_channel_id": -1001112223334}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            env = {"TG_API_ID": "12345", "TG_API_HASH": "test-env-hash", "TG_PROXY": "http://localhost:8080",
                   "HTTP_PROXY_URL": "http://localhost:8081", "JAVHD_COOKIES_FILE": "own-cookies.txt"}
            with patch.object(common, "ROOT", root), patch.dict(os.environ, env, clear=True):
                loaded = common.load_config()
            self.assertEqual(loaded["telegram_api_id"], 12345)
            self.assertEqual(loaded["telegram_api_hash"], env["TG_API_HASH"])
            self.assertEqual(loaded["telegram_proxy"], env["TG_PROXY"])
            self.assertEqual(loaded["http_proxy"], env["HTTP_PROXY_URL"])
            self.assertEqual(loaded["javhd_cookies_file"], env["JAVHD_COOKIES_FILE"])
            self.assertEqual(loaded["telegram_channel_id"], -1001112223334)


class ChannelPeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_entity_lookup_uses_configured_channel(self):
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"telegram_channel_id": -1001112223334}
        uploader.client = SimpleNamespace(get_input_entity=AsyncMock(return_value="configured-peer"))
        self.assertEqual(await uploader.peer(), "configured-peer")
        uploader.client.get_input_entity.assert_awaited_once_with(-1001112223334)

    async def test_dialog_fallback_uses_configured_channel(self):
        async def dialogs():
            yield SimpleNamespace(id=-1009998887776, input_entity="wrong")
            yield SimpleNamespace(id=-1001112223334, input_entity="right")
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"telegram_channel_id": -1001112223334}
        uploader.client = SimpleNamespace(get_input_entity=AsyncMock(side_effect=ValueError()), iter_dialogs=dialogs)
        self.assertEqual(await uploader.peer(), "right")

    async def test_unconfigured_channel_does_not_contact_telegram(self):
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = {"telegram_channel_id": 0}
        uploader.client = SimpleNamespace(get_input_entity=AsyncMock())
        with self.assertRaises(common.JobError):
            await uploader.peer()
        uploader.client.get_input_entity.assert_not_awaited()


class DistributionTests(unittest.TestCase):
    def make_source(self, directory: Path) -> Path:
        root = directory / "source"
        for name in package_skill.FILES:
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if name == "config.example.json":
                destination.write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
            else:
                shutil.copyfile(ROOT / name, destination)
        return root

    def test_default_archive_is_inside_skill_directory(self):
        with patch.object(package_skill, "build_archive", return_value={"ok": True}) as build:
            with redirect_stdout(StringIO()):
                self.assertEqual(package_skill.main([]), 0)
        build.assert_called_once_with(package_skill.ROOT, package_skill.ROOT / "dist" / "javhd-skill.zip",
                                      overwrite=False)

    def test_standard_metadata_and_relative_resources(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        header, body = text.split("---", 2)[1:]
        name = re.search(r"(?m)^name: (.+)$", header)[1]
        description = re.search(r"(?m)^description: (.+)$", header)[1]
        compatibility = re.search(r"(?m)^compatibility: (.+)$", header)[1]
        self.assertEqual(name, package_skill.SKILL_NAME)
        self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(description), 1024)
        self.assertLessEqual(len(compatibility), 500)
        self.assertNotIn("argument-hint", header)
        self.assertNotIn("$ARGUMENTS", body)
        for relative in re.findall(r"\]\(([^)]+)\)", body):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_allowlist_excludes_cookies_and_runtime_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = self.make_source(directory)
            marker = uuid.uuid4().hex
            for name in ("private-cookies.txt", ".state/telegram.session", ".state/quark/cookie.json",
                         ".venv/private.txt", "downloads/video.mp4", "private.env", "config.backup.json"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(marker, encoding="utf-8")
            output = directory / "bundle.zip"
            info = package_skill.build_archive(root, output)
            self.assertEqual(info["files"], len(package_skill.FILES))
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {f"javhd/{name}" for name in package_skill.FILES})
                self.assertNotIn(marker.encode(), b"".join(archive.read(name) for name in archive.namelist()))

    def test_local_config_is_preserved_and_never_distributed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = self.make_source(directory)
            config = {**TEST_CONFIG, "telegram_api_id": 12345, "telegram_api_hash": uuid.uuid4().hex,
                      "telegram_channel_id": -1001112223334, "telegram_proxy": "socks5://localhost:1080",
                      "http_proxy": "http://localhost:8080", "javhd_cookies_file": "private-cookies.txt"}
            path = root / "config.json"
            path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            original = path.read_bytes()
            output = directory / "bundle.zip"
            package_skill.build_archive(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertNotIn("javhd/config.json", archive.namelist())
                self.assertEqual(json.loads(archive.read("javhd/config.example.json")), TEST_CONFIG)
                data = b"".join(archive.read(name) for name in archive.namelist())
                self.assertNotIn(config["telegram_api_hash"].encode(), data)
            self.assertEqual(path.read_bytes(), original)

    def test_missing_local_config_does_not_block_packaging(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = self.make_source(directory)
            self.assertFalse((root / "config.json").exists())
            output = directory / "bundle.zip"
            package_skill.build_archive(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("javhd/config.example.json", archive.namelist())
                self.assertNotIn("javhd/config.json", archive.namelist())
            self.assertFalse((root / "config.json").exists())

    def test_missing_template_is_not_packaged(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = self.make_source(directory)
            (root / "config.example.json").unlink()
            output = directory / "bundle.zip"
            with self.assertRaisesRegex(ValueError, "config.example.json"):
                package_skill.build_archive(root, output)
            self.assertFalse(output.exists())

    def test_invalid_template_json_is_not_packaged(self):
        for text in ("{", "[]"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                root = self.make_source(directory)
                (root / "config.example.json").write_text(text, encoding="utf-8")
                output = directory / "bundle.zip"
                with self.assertRaises(ValueError):
                    package_skill.build_archive(root, output)
                self.assertFalse(output.exists())

    def test_filled_template_is_rejected_without_echoing_values(self):
        for key in ("telegram_api_id", "telegram_api_hash", "telegram_channel_id", "telegram_proxy",
                    "http_proxy", "javhd_cookies_file"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                root = self.make_source(directory)
                secret = uuid.uuid4().hex
                value = 12345 if key == "telegram_api_id" else -1001112223334 if key == "telegram_channel_id" else secret
                template = {**TEST_CONFIG, key: value}
                (root / "config.example.json").write_text(json.dumps(template), encoding="utf-8")
                output = directory / "bundle.zip"
                with self.assertRaisesRegex(ValueError, key) as raised:
                    package_skill.build_archive(root, output)
                self.assertNotIn(str(value), str(raised.exception))
                self.assertFalse(output.exists())

    def test_incomplete_or_unexpected_template_fields_are_rejected(self):
        missing = dict(TEST_CONFIG)
        missing.pop("telegram_api_hash")
        for template in (missing, {**TEST_CONFIG, "extra_secret": uuid.uuid4().hex},
                         {**TEST_CONFIG, "telegram_api_id": False},
                         {**TEST_CONFIG, "upload_concurrency": 0},
                         {**TEST_CONFIG, "upload_concurrency": True},
                         {**TEST_CONFIG, "notify_saved_messages": "true"}):
            with self.subTest(template_keys=list(template)), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                root = self.make_source(directory)
                (root / "config.example.json").write_text(json.dumps(template), encoding="utf-8")
                output = directory / "bundle.zip"
                with self.assertRaises(ValueError):
                    package_skill.build_archive(root, output)
                self.assertFalse(output.exists())

    def test_existing_archive_is_not_overwritten_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = self.make_source(directory)
            output = directory / "bundle.zip"
            output.write_bytes(b"keep-existing")
            with self.assertRaises(FileExistsError):
                package_skill.build_archive(root, output)
            self.assertEqual(output.read_bytes(), b"keep-existing")

    def test_extracted_skill_runs_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = self.make_source(directory)
            archive_path = directory / "bundle.zip"
            package_skill.build_archive(root, archive_path)
            extracted = directory / "install"
            extracted.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extracted)
            cwd = directory / "unrelated"
            cwd.mkdir()
            env = os.environ.copy()
            for key in ("TG_API_ID", "TG_API_HASH", "TG_PROXY", "HTTP_PROXY_URL", "JAVHD_COOKIES_FILE"):
                env.pop(key, None)
            skill_root = extracted / "javhd"
            command = [sys.executable, "-I", str(skill_root / "scripts/main.py"), "--check"]
            missing = subprocess.run(
                command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(missing.returncode, 1, missing.stderr + missing.stdout)
            self.assertIn("config.example.json", json.loads(missing.stdout)["message"])
            self.assertFalse((skill_root / "config.json").exists())
            for relative in re.findall(r"\]\(([^)]+)\)", (skill_root / "SKILL.md").read_text(encoding="utf-8")):
                self.assertTrue((skill_root / relative).is_file(), relative)
            shutil.copyfile(skill_root / "config.example.json", skill_root / "config.json")
            original = (skill_root / "config.json").read_bytes()
            completed = subprocess.run(
                command,
                cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            result = json.loads(completed.stdout)
            self.assertTrue(result["native_enabled"])
            self.assertEqual(result["quark_cli_version"], "1.0.20")
            self.assertTrue(result["node"].startswith("v"))
            self.assertFalse(result["telegram_credentials_ready"])
            self.assertFalse(result["telegram_channel_ready"])
            self.assertEqual((skill_root / "config.json").read_bytes(), original)
            self.assertEqual(list(cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
