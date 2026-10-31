"""批量输入映射、详情页 URL 优先和重试保留；全部使用本地模拟响应。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import FORMAT_HINT, JobError, detail_page_url, parse_requests
import main
import probe_video
import quark
import source
import telegram
from task_files import MANIFEST, TaskFiles
from d1_testing import install_capture

URL = "https://javhd.com/zh/video/custom-slug?from=list&view=full"
FULL = "https://cdn.example/full.mp4?temporary=private"
HTML = '''<h1>中文视频标题</h1><meta property="og:image" content="https://cdn.example/cover.jpg">
<player-component content-path="/zh/player/987?is_trailer=1&amp;extra=keep"
 video-preview="https://cdn.example/preview.mp4"></player-component>'''


class RequestInputTests(unittest.TestCase):
    def setUp(self):
        self.capture = install_capture(self)

    def test_id_lists_share_tags_and_keep_order(self):
        for value in ("ID：123,456、789 标签：#4k", "id:123 456\n789 标签:#4k", "Id:123，456,789 标签：#4k"):
            with self.subTest(value=value):
                requests = parse_requests(value)
                self.assertEqual([item.video_id for item in requests], ["123", "456", "789"])
                self.assertTrue(all(item.tags == ["#4k"] and item.page_url == "" for item in requests))

    def test_multiline_url_and_tags_belong_to_their_own_id(self):
        requests = parse_requests(f"ID：123 URL：{URL} 标签：#4k\nID:456 标签:#高清\nID：789 url:https://javhd.com/zh/another")
        self.assertEqual([(item.video_id, item.tags, item.page_url) for item in requests], [
            ("123", ["#4k"], URL), ("456", ["#高清"], ""), ("789", [], "https://javhd.com/zh/another")])

    def test_duplicate_ids_merge_nonconflicting_url_and_tags_without_duplicate_upload(self):
        requests = parse_requests(f"id:123,456 标签:#123\nid:123 URL:{URL} 标签:#4k")
        self.assertEqual([item.video_id for item in requests], ["123", "456"])
        self.assertEqual(requests[0].tags, ["#ID123", "#4k"])
        self.assertEqual(requests[0].page_url, URL)

    def test_invalid_or_ambiguous_batch_never_reads_config_or_starts_jobs(self):
        bad = ("id:123 id:0", "id:123 id:abc", f"id:123,456 URL:{URL}", "id:123 URL:",
               f"id:123 URL:{URL} URL:{URL}", f"id:123 URL:{URL}\nid:123 URL:https://javhd.com/other",
               "id:123 标签:#4k;whoami", "id:123 url:https://evil.example/video", "URL:" + URL,
               "id:123 URL:https://javhd.com@evil.example/", "id:123 URL:https://user:secret@javhd.com/video")
        for value in bad:
            with self.subTest(value=value), patch.object(main, "load_config") as config, \
                    patch.object(main, "operate_batch") as batch, redirect_stdout(StringIO()) as output:
                self.assertEqual(main.main(["--input", value]), 2)
                self.assertEqual(output.getvalue(), FORMAT_HINT + "\n")
                config.assert_not_called()
                batch.assert_not_called()

    def test_cli_ids_and_single_url_reach_correct_operations(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "STATE", Path(temp)), \
                patch.object(main, "load_config", return_value={}), \
                patch.object(main, "operate_batch", new=AsyncMock(return_value=0)) as batch, \
                patch.object(main, "operate", new=AsyncMock(return_value=0)) as single:
            self.assertEqual(main.main(["--ids", "123", "456", "--tags", "#4k"]), 0)
            self.assertEqual([item.video_id for item in batch.call_args.args[1]], ["123", "456"])
            single.assert_not_awaited()
            self.assertEqual(main.main(["--id", "789", "--url", URL]), 0)
            self.assertEqual(single.call_args.kwargs["video_id"], "789")
            self.assertEqual(single.call_args.kwargs["page_url"], URL)

    def test_cli_does_not_guess_url_to_id_association(self):
        for args in (["--ids", "123", "456", "--url", URL], ["--url", URL], ["--id", "123", "--url", ""]):
            with self.subTest(args=args), patch.object(main, "load_config") as config, redirect_stdout(StringIO()):
                self.assertEqual(main.main(args), 2)
                config.assert_not_called()

    def test_urls_keep_query_and_reject_non_site_credentials_and_control_characters(self):
        self.assertEqual(detail_page_url(URL), URL)
        self.assertEqual(detail_page_url("https://www.javhd.com/zh/slug"), "https://www.javhd.com/zh/slug")
        for value in ("//javhd.com/zh/a", "file:///etc/passwd", "https://javhd.com.evil.example/a",
                      "https://javhd.com:9999/", "https://javhd.com/abc\nURL:bad", "https://javhd.com\\evil.example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detail_page_url(value)


class DetailURLTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = install_capture(self)

    async def test_explicit_url_is_requested_directly_and_internal_player_id_is_preserved(self):
        seen = []
        def handler(request):
            seen.append(request)
            if str(request.url) == URL:
                return httpx.Response(200, text=HTML)
            if request.url.path == "/zh/player/987":
                self.assertEqual(request.url.params["is_trailer"], "0")
                self.assertEqual(request.url.params["extra"], "keep")
                return httpx.Response(200, json={"sources": [{"src": FULL}]})
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            info = await source.resolve(client, "123", page_url=URL)
        self.assertEqual(info.video_id, "123")
        self.assertEqual(info.page_url, URL)
        self.assertEqual(info.video_url, FULL)
        self.assertEqual(len(seen), 2)
        self.assertFalse(any(request.url.path == "/zh/id/123" for request in seen))

    async def test_explicit_404_has_no_generated_id_fallback(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await source.resolve(client, "123", page_url=URL)
        self.assertEqual(seen, [URL])

    async def test_invalid_url_is_rejected_before_any_request(self):
        client = AsyncMock()
        with self.assertRaises(JobError):
            await source.resolve(client, "123", page_url="https://other.example/")
        client.stream.assert_not_called()

    async def test_preparation_forwards_url_to_resolver_before_downloading(self):
        with patch.object(source, "website_client") as factory, \
                patch.object(source, "resolve", new=AsyncMock(side_effect=JobError("stop after resolve"))) as resolve:
            factory.return_value.__aenter__ = AsyncMock(return_value="client")
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with self.assertRaisesRegex(JobError, "stop after resolve"):
                await source.prepare_media("123", Path("unused"), {}, page_url=URL)
            resolve.assert_awaited_once_with("client", "123", page_url=URL)

    async def test_url_remains_in_manifest_for_retry_after_preparation_failure(self):
        attempts = []
        async def prepare(video_id, directory, config, **kwargs):
            attempts.append(kwargs["page_url"])
            (directory / "keep.bin").write_bytes(b"keep")
            raise JobError("preparation failed")
        tg = SimpleNamespace(login=AsyncMock(), upload=AsyncMock(), notify=AsyncMock(), close=AsyncMock())
        q = SimpleNamespace(login=MagicMock(), upload=MagicMock(), close=MagicMock())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {"notify_saved_messages": False, "telegram_channel_id": -100123}
            with patch.object(main, "ROOT", root), patch.object(main, "STATE", root / ".state"), \
                    patch.object(source, "prepare_media", side_effect=prepare), \
                    patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark", return_value=q), \
                    redirect_stdout(StringIO()):
                self.assertEqual(await main.operate(config, video_id="123", page_url=URL), 1)
                result = self.capture.result("123")
                task = TaskFiles.load(root, Path(result["retained_dir"]), retry=True)
                self.assertEqual(task.data["page_url"], URL)
                self.assertNotIn(FULL, (task.directory / MANIFEST).read_text(encoding="utf-8"))
                self.assertEqual(await main.operate(config, video_id="123", retained_task=task), 1)
            self.assertEqual(attempts, [URL, URL])
            tg.upload.assert_not_awaited()
            q.upload.assert_not_called()

    async def test_probe_accepts_same_explicit_page_url(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            if str(request.url) == URL:
                return httpx.Response(200, text=HTML)
            return httpx.Response(200, json={"sources": [{"src": FULL}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await probe_video.probe(client, "123", page_url=URL)
        self.assertTrue(result["success"])
        self.assertEqual(seen[0], URL)
        self.assertEqual(result["page_url"], URL)
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
