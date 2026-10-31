"""Cloudflare REST 协议、真实 SQLite/Snowflake 及上传元数据；不访问线上账号。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import JobError
import common
import main
import package_skill
import quark
from records import EPOCH, METADATA_FIELDS, UploadRecords, metadata_from_info, validate_config
import source
import telegram
from d1_testing import Capture, CONFIG, install_capture
from test_retained_tasks import media_files


class RestTests(unittest.TestCase):
    def test_bearer_path_sql_and_params_use_official_rest_contract(self):
        seen = []
        def response(request):
            seen.append(request)
            return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [{"ok": 1}]}]})
        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            records = UploadRecords(CONFIG, client=client)
            self.assertEqual(records.query("SELECT ? AS ok", ("value",)), [{"ok": 1}])
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://api.cloudflare.com/client/v4/accounts/" + CONFIG["ACCOUNT_ID"]
                         + "/d1/database/" + CONFIG["DATABASE_ID"] + "/query")
        self.assertEqual(request.headers["Authorization"], "Bearer " + CONFIG["API_TOKEN"])
        self.assertEqual(json.loads(request.content), {"sql": "SELECT ? AS ok", "params": ["value"]})

    def test_outer_and_inner_success_and_http_status_are_all_required(self):
        payloads = [(403, {"success": True, "result": [{"success": True}]}),
                    (200, {"success": False, "result": [{"success": True}]}),
                    (200, {"success": True, "result": [{"success": False}]}),
                    (200, {"success": True, "result": []}),
                    (200, {"success": True, "result": [{"success": True, "results": "invalid"}]}),
                    (200, {"success": True, "errors": [{"code": 10000, "message": CONFIG["API_TOKEN"]}],
                           "result": [{"success": True}]}), (200, [])]
        for status, payload in payloads:
            with self.subTest(status=status, payload=payload), httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json=payload))) as client:
                with self.assertRaises(JobError) as caught:
                    UploadRecords(CONFIG, client=client).query("SELECT 1")
                self.assertNotIn(CONFIG["API_TOKEN"], str(caught.exception))

    def test_timeout_and_transport_errors_do_not_echo_token_url_sql_or_parameters(self):
        for error in (httpx.ReadTimeout("Bearer " + CONFIG["API_TOKEN"]), httpx.ConnectError("private URL")):
            def fail(request):
                raise error
            with self.subTest(error=type(error).__name__), httpx.Client(transport=httpx.MockTransport(fail)) as client:
                with self.assertRaises(JobError) as caught:
                    UploadRecords(CONFIG, client=client).query("SELECT ?", ("private parameter",))
                self.assertNotIn(CONFIG["API_TOKEN"], str(caught.exception))
                self.assertNotIn("private", str(caught.exception))

    def test_invalid_json_and_redirect_are_not_success(self):
        for status, content in ((200, b"not json"), (302, b"redirect")):
            with self.subTest(status=status), httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, content=content))) as client:
                with self.assertRaises(JobError):
                    UploadRecords(CONFIG, client=client).query("SELECT 1")

    def test_missing_credentials_or_invalid_instance_fail_locally(self):
        for config in ({}, {**CONFIG, "ACCOUNT_ID": "../other"}, {**CONFIG, "DATABASE_ID": "invalid"},
                       {**CONFIG, "API_TOKEN": "secret\nInjected: yes"}, {**CONFIG, "SNOWFLAKE_INSTANCE_ID": 1024},
                       {**CONFIG, "SNOWFLAKE_INSTANCE_ID": True}):
            with self.subTest(config_keys=list(config)), self.assertRaises(JobError):
                UploadRecords(config)
        self.assertFalse(validate_config({}))


class SqliteRecordTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        self.addCleanup(self.capture.close)

    def test_schema_has_requested_fields_unique_video_and_zero_one_constraints(self):
        columns = self.capture.connection.execute("PRAGMA table_info(video_uploads)").fetchall()
        self.assertEqual([row["name"] for row in columns], ["id", "video_id", *METADATA_FIELDS,
                         "telegram_status", "quark_status", "telegram_message_ids", "quark_file_id"])
        record_id = self.capture.records.begin("123")
        with self.assertRaises(sqlite3.IntegrityError):
            self.capture.connection.execute("UPDATE video_uploads SET telegram_status=2 WHERE id=?", (record_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.capture.connection.execute("INSERT INTO video_uploads(id,video_id) VALUES('another','123')")

    def test_parameterized_metadata_round_trips_without_executing_input(self):
        records = self.capture.records
        record_id = records.begin("123", "https://javhd.com/zh/id/123")
        values = {"title_zh": "中文'标题; DROP TABLE video_uploads; --", "title_en": 'English "title"',
                  "detail_url": "https://javhd.com/zh/studio/room/example/video/123", "cover_url": "https://cdn.example/cover.jpg",
                  "preview_url": "https://cdn.example/preview.mp4?sample=1"}
        records.metadata(record_id, values)
        for field, value in values.items():
            self.assertEqual(self.capture.row("123")[field], value)
        self.assertEqual(len(self.capture.rows()), 1)

    def test_snowflake_ids_are_unique_and_kept_as_exact_decimal_text(self):
        from snowflake import Snowflake
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda number: self.capture.records.begin(str(number)), range(1, 61)))
        self.assertEqual(len(set(ids)), 60)
        self.assertTrue(all(isinstance(value, str) and value.isdigit() for value in ids))
        parsed = Snowflake.parse(int(ids[0]), EPOCH)
        self.assertEqual(parsed.instance, CONFIG["SNOWFLAKE_INSTANCE_ID"])
        self.assertEqual(self.capture.connection.execute("SELECT typeof(id) FROM video_uploads LIMIT 1").fetchone()[0], "text")

    def test_large_snowflake_is_sent_as_string_without_json_number_rounding(self):
        records = UploadRecords(CONFIG, client=self.capture.client, generator=iter([9007199254740993]))
        self.assertEqual(records.begin("123"), "9007199254740993")
        insert = next(item for item in reversed(self.capture.requests) if item["sql"].startswith("INSERT"))
        self.assertEqual(insert["params"][0], "9007199254740993")

    def test_updates_for_two_platforms_cannot_overwrite_each_other(self):
        record_id = self.capture.records.begin("123")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.capture.records.platform, record_id, "telegram", True, [101, 102]),
                       pool.submit(self.capture.records.platform, record_id, "quark", True, "quark-123")]
            for future in futures:
                future.result()
        row = self.capture.row("123")
        self.assertEqual((row["telegram_status"], row["quark_status"]), (1, 1))
        self.assertEqual(row["telegram_message_ids"], "101,102")
        self.assertEqual(row["quark_file_id"], "quark-123")

    def test_retry_keeps_success_and_new_attempt_resets_latest_status_but_keeps_primary_key(self):
        records = self.capture.records
        record_id = records.begin("123")
        records.platform(record_id, "telegram", True, [101])
        self.assertEqual(records.begin("123", record_id=record_id, retry=True), record_id)
        self.assertEqual(self.capture.row("123")["telegram_status"], 1)
        self.assertEqual(records.begin("123"), record_id)
        self.assertEqual(self.capture.row("123")["telegram_status"], 0)
        self.assertEqual(self.capture.row("123")["telegram_message_ids"], "")

    def test_missing_record_wrong_video_or_success_without_remote_id_is_refused(self):
        record_id = self.capture.records.begin("123")
        with self.assertRaises(JobError):
            self.capture.records.begin("456", record_id=record_id, retry=True)
        for service, ids in (("telegram", []), ("telegram", [True]), ("quark", ""), ("other", "id")):
            with self.subTest(service=service, ids=ids), self.assertRaises(JobError):
                self.capture.records.platform(record_id, service, True, ids)

    def test_returned_numeric_or_mismatched_id_is_not_accepted(self):
        for rows in ([], [{"id": 9007199254740992}], [{"id": "999"}]):
            with self.subTest(rows=rows), self.assertRaises(JobError):
                UploadRecords._confirmed(rows, "123")


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def test_native_english_title_from_current_player_is_saved_with_all_requested_metadata(self):
        page = "https://javhd.com/zh/id/123"
        html = '''<h1>中文原生标题</h1><meta property="og:image" content="https://cdn.example/cover.jpg">
        <player-component content-path="/zh/player_api?videoId=77" video-preview="https://cdn.example/preview.mp4"></player-component>'''
        def handler(request):
            return httpx.Response(200, text=html) if request.url.path == "/zh/id/123" else httpx.Response(
                200, json={"title": "Native English title", "sources": [{"src": "https://cdn.example/full.mp4?private=1"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            info = await source.resolve(client, "123")
        self.assertEqual(info.title_en, "Native English title")
        record_id = self.capture.records.begin("123")
        self.capture.records.metadata(record_id, metadata_from_info(info))
        row = self.capture.row("123")
        self.assertEqual(row["title_zh"], "中文原生标题")
        self.assertEqual(row["title_en"], "Native English title")
        self.assertEqual(row["detail_url"], page)
        self.assertEqual(row["preview_url"], "https://cdn.example/preview.mp4")
        self.assertNotIn("private=1", json.dumps(row))

    async def test_metadata_callback_runs_before_any_complete_video_download(self):
        info = source.VideoInfo("123", "中文标题", "https://javhd.com/zh/id/123", "", "", "https://cdn.example/full.mp4")
        callback = AsyncMock(side_effect=JobError("stop before download"))
        with patch.object(source, "website_client") as factory, patch.object(source, "resolve", new=AsyncMock(return_value=info)), \
                patch.object(source, "download", new=AsyncMock()) as download:
            factory.return_value.__aenter__ = AsyncMock(return_value="client")
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with self.assertRaisesRegex(JobError, "stop before download"):
                await source.prepare_media("123", Path("unused"), {}, on_resolved=callback)
            callback.assert_awaited_once_with(info)
            download.assert_not_awaited()

    async def test_partial_telegram_video_ids_are_saved_with_failed_status_and_preview_excluded(self):
        async def prepare(video_id, directory, config, **kwargs):
            return media_files(directory, video_id)
        async def fail_upload(media, tags):
            tg.video_message_ids = [101, 102]
            raise JobError("later album failed")
        tg = SimpleNamespace(login=AsyncMock(), upload=fail_upload, notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=MagicMock(return_value="quark-ok"), close=MagicMock())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(source, "prepare_media", side_effect=prepare), \
                    patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), redirect_stdout(StringIO()):
                self.assertEqual(await main.operate({"notify_saved_messages": False}, video_id="123"), 1)
            row = self.capture.row("123")
            self.assertEqual(row["telegram_status"], 0)
            self.assertEqual(row["telegram_message_ids"], "101,102")
            self.assertEqual(row["quark_status"], 1)
            self.assertTrue(self.capture.result("123")["db_recorded"])
            self.assertFalse(list((root / ".state").glob("result-*.json")))


class ConfigAndInitTests(unittest.TestCase):
    def setUp(self):
        self.capture = install_capture(self)

    def test_init_db_is_separate_from_upload_and_prints_only_ready_state(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "STATE", Path(temp)), \
                patch.object(main, "load_config", return_value=CONFIG), patch.object(main, "operate") as operate:
            self.assertEqual(main.main(["--init-db"]), 0)
            operate.assert_not_called()
        self.assertTrue(self.capture.result(event="database_ready")["ready"])
        self.assertNotIn(CONFIG["API_TOKEN"], json.dumps(self.capture.events))

    def test_blank_template_and_local_config_never_print_api_token(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual([config[name] for name in ("ACCOUNT_ID", "DATABASE_ID", "API_TOKEN")], ["", "", ""])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.json").write_text(json.dumps({**config, **CONFIG}), encoding="utf-8")
            with patch.object(common, "ROOT", root):
                self.assertEqual(common.load_config()["API_TOKEN"], CONFIG["API_TOKEN"])


if __name__ == "__main__":
    unittest.main()
