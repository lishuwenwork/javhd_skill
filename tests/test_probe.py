"""解析诊断的离线回归；真实网站测试由 scripts/probe_video.py 单独运行。"""

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import httpx
from PIL import Image

import probe_video
from probe_video import probe, save_assets, verify_video
from source import inspect_detail, resolve, resolve_preview

PAGE = "https://javhd.com/zh/id/59152"
HTML = '''<h1>测试中文标题</h1><meta property="og:image" content="https://cdn.example/cover.png">
<player-component role="MEMBER" content-path="/zh/player_api?videoId=77&amp;is_trailer=0"
 video-preview="https://cdn.example/preview.gif"></player-component>'''
FULL = "https://cdn.example/full.mp4?signature=example"


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, html=HTML, playback=None, preview_status=404, preview_body=b"", playback_status=200):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/zh/id/59152":
                return httpx.Response(200, text=html)
            if request.url.path == "/zh/player_api":
                self.assertEqual(request.headers.get("X-Requested-With"), "XMLHttpRequest")
                return httpx.Response(playback_status, json=playback if playback is not None else {
                    "sources": [{"src": FULL, "res": 1080}]})
            if request.url.path == "/share2/trailers/59152/5sec_1000_n.mp4":
                return httpx.Response(preview_status, stream=httpx.ByteStream(preview_body))
            self.fail("不应下载完整视频或请求其他资源：" + str(request.url))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await probe(client, "59152")
        return result, requests

    async def test_all_four_fields_and_no_complete_video_request(self):
        result, requests = await self.collect()
        self.assertTrue(result["success"])
        self.assertTrue(all(status == "ok" for status in result["checks"].values()))
        self.assertEqual(result["full_video_url"], FULL)
        self.assertEqual(result["gif_url"], "https://cdn.example/preview.gif")
        self.assertEqual(len(requests), 2)

    async def test_guest_trailer_page_uses_full_mode_and_internal_id(self):
        html = HTML.replace("is_trailer=0", "is_trailer=1").replace('role="MEMBER"', 'role="GUEST"')
        result, requests = await self.collect(html)
        self.assertTrue(result["success"])
        self.assertEqual(result["full_video_url"], FULL)
        self.assertEqual(requests[1].url.params["is_trailer"], "0")
        self.assertEqual(requests[1].url.params["videoId"], "77")  # 不是公开 ID 59152。
        self.assertTrue(result["playback"]["page_is_trailer"])
        self.assertFalse(result["playback"]["is_trailer"])
        self.assertEqual(len(requests), 2)

    async def test_trailer_only_response_does_not_hide_title_and_cover(self):
        result, requests = await self.collect(playback={"sources": [{
            "src": "https://c4.cdnjhd.com/share2/trailers/59152/1min_4000_n.mp4"}]})
        self.assertFalse(result["success"])
        self.assertEqual(result["checks"]["title_zh"], "ok")
        self.assertEqual(result["checks"]["cover_url"], "ok")
        self.assertEqual(result["checks"]["full_video_url"], "missing")
        self.assertEqual(result["full_video_url"], "")
        self.assertEqual(len(requests), 2)

    async def test_foreign_title_is_not_silently_translated(self):
        result, _ = await self.collect(HTML.replace("测试中文标题", "English title"))
        self.assertEqual(result["title_zh"], "")
        self.assertEqual(result["checks"]["title_zh"], "missing")
        self.assertEqual(result["checks"]["full_video_url"], "ok")

    async def test_mp4_preview_is_not_misreported_as_gif(self):
        result, _ = await self.collect(HTML.replace("preview.gif", "preview.mp4"))
        self.assertEqual(result["preview_format"], "mp4")
        self.assertEqual(result["gif_url"], "")
        self.assertEqual(result["checks"]["preview_url"], "ok")
        self.assertTrue(result["success"])

    async def test_cdn_preview_is_checked_for_current_public_id(self):
        html = HTML.replace(' video-preview="https://cdn.example/preview.gif"', "")
        result, requests = await self.collect(html, preview_status=206,
                                              preview_body=b"\x00\x00\x00\x20ftyp" + b"0" * 56)
        self.assertTrue(result["success"])
        self.assertEqual(result["gif_url"], "")
        self.assertEqual(result["preview_format"], "mp4")
        self.assertEqual(result["preview_url"],
                         "https://c4.cdnjhd.com/share2/trailers/59152/5sec_1000_n.mp4")
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[-1].headers["Range"], "bytes=0-63")
        self.assertEqual(requests[-1].headers["Referer"], PAGE)

    async def test_missing_or_html_cdn_preview_is_not_reported_as_success(self):
        html = HTML.replace(' video-preview="https://cdn.example/preview.gif"', "")
        for status, body in ((404, b""), (200, b"<html>not a video</html>")):
            with self.subTest(status=status):
                result, _ = await self.collect(html, preview_status=status, preview_body=body)
                self.assertFalse(result["success"])
                self.assertEqual(result["preview_url"], "")
                self.assertEqual(result["checks"]["preview_url"], "missing")
                self.assertEqual(result["full_video_url"], FULL)

    async def test_preview_read_is_bounded_even_when_cdn_ignores_range(self):
        chunks = []
        class VideoStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                chunks.append(1)
                yield b"\x00\x00\x00\x20ftyp" + b"0" * 56
                chunks.append(2)
                yield b"0" * 1024
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=VideoStream()))
        async with httpx.AsyncClient(transport=transport) as client:
            url = await resolve_preview(client, "59152", PAGE)
        self.assertTrue(url.endswith("/59152/5sec_1000_n.mp4"))
        self.assertEqual(chunks, [1])

    async def test_access_denied_preserves_metadata_without_retrying_trailer(self):
        result, requests = await self.collect(playback_status=403)
        self.assertFalse(result["success"])
        self.assertEqual(result["title_zh"], "测试中文标题")
        self.assertEqual(result["preview_url"], "https://cdn.example/preview.gif")
        self.assertEqual(result["full_video_url"], "")
        self.assertEqual(result["notes"]["full_video_url"], "HTTP 403")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].url.params["is_trailer"], "0")

    async def test_upload_resolver_uses_same_full_source_and_preview_flow(self):
        html = HTML.replace("is_trailer=0", "is_trailer=1")
        def handler(request):
            if request.url.path == "/zh/id/59152":
                return httpx.Response(200, text=html)
            self.assertEqual(request.url.params["is_trailer"], "0")
            self.assertEqual(request.url.params["videoId"], "77")
            return httpx.Response(200, json={"sources": [{"src": FULL}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            info = await resolve(client, "59152")
        self.assertEqual(info.video_url, FULL)
        self.assertEqual(info.preview_url, "https://cdn.example/preview.gif")

    async def test_page_duration_mismatch_does_not_clear_full_source(self):
        result, _ = await self.collect()
        result["expected_duration_seconds"] = 1322
        with patch.object(probe_video, "inspect_video_url", new=AsyncMock(return_value={
                "duration_seconds": 760.307})):
            await verify_video(AsyncMock(), result)
        self.assertTrue(result["success"])
        self.assertEqual(result["full_video_url"], FULL)
        self.assertEqual(result["video_verification"]["status"], "mp4_confirmed")
        self.assertFalse(result["video_verification"]["page_duration_matches"])

    async def test_invalid_mp4_response_still_fails_verification(self):
        result, _ = await self.collect()
        with patch.object(probe_video, "inspect_video_url", new=AsyncMock(side_effect=
                probe_video.JobError("视频链接没有返回可识别的 MP4 文件头"))):
            await verify_video(AsyncMock(), result)
        self.assertFalse(result["success"])
        self.assertEqual(result["full_video_url"], "")
        self.assertEqual(result["checks"]["full_video_url"], "verification_error")

    async def test_playback_poster_can_fill_missing_cover(self):
        html = HTML.replace('<meta property="og:image" content="https://cdn.example/cover.png">', "")
        result, _ = await self.collect(html, {"poster": "https://cdn.example/poster.jpg",
                                              "sources": [{"src": FULL}]})
        self.assertEqual(result["cover_url"], "https://cdn.example/poster.jpg")

    def test_recommended_video_is_not_used_as_current_preview(self):
        html = HTML.replace(' video-preview="https://cdn.example/preview.gif"', "")
        html += '<thumb-component link-content="/zh/id/999" video-preview="https://cdn.example/wrong.gif"></thumb-component>'
        result = inspect_detail(html, PAGE, "59152")
        self.assertEqual(result["preview_url"], "")

    async def test_playback_error_preserves_independent_metadata(self):
        result, _ = await self.collect(playback={"sources": []})
        self.assertEqual(result["checks"]["title_zh"], "ok")
        self.assertEqual(result["checks"]["cover_url"], "ok")
        self.assertEqual(result["full_video_url"], "")
        self.assertIn("error", result["playback"])

    async def test_http_error_is_reported_without_secret_url(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(403))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await probe(client, "59152")
        self.assertFalse(result["success"])
        self.assertEqual(result["notes"]["page"], "HTTP 403")
        self.assertTrue(all(status == "missing" for status in result["checks"].values()))

    async def test_save_only_cover_and_existing_preview_in_separate_directories(self):
        cover, gif = BytesIO(), BytesIO()
        Image.new("RGB", (32, 32), "blue").save(cover, "PNG")
        Image.new("RGB", (32, 32), "blue").save(
            gif, "GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "red")], duration=100, loop=0)
        def handler(request):
            if request.url.path == "/cover.png":
                return httpx.Response(200, content=cover.getvalue(), headers={"content-type": "image/png"})
            if request.url.path == "/preview.gif":
                return httpx.Response(200, content=gif.getvalue(), headers={"content-type": "image/gif"})
            self.fail("不能下载完整视频")
        result = {"page_url": PAGE, "cover_url": "https://cdn.example/cover.png",
                  "preview_url": "https://cdn.example/preview.gif", "gif_url": "https://cdn.example/preview.gif"}
        with tempfile.TemporaryDirectory() as temp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await save_assets(client, result, Path(temp))
            self.assertEqual(result["asset_errors"], {})
            self.assertTrue(Path(result["files"]["cover"]).is_file())
            with Image.open(result["files"]["gif"]) as image:
                self.assertEqual(image.n_frames, 2)
            self.assertNotEqual(Path(result["files"]["cover"]).parent, Path(result["files"]["gif"]).parent)


class ProbeCliTests(unittest.TestCase):
    def test_default_four_lines_and_explicit_json_output(self):
        for detailed in (False, True):
            with self.subTest(json=detailed), tempfile.TemporaryDirectory() as temp:
                result = {"id": "64584", "title_zh": "测试标题", "cover_url": "https://cdn.example/cover.jpg",
                          "preview_url": "https://cdn.example/preview.mp4", "preview_format": "mp4",
                          "full_video_url": FULL, "success": True}
                args = ["--id", "64584", "--output", str(Path(temp) / "probe")]
                if detailed:
                    args.append("--json")
                with patch.object(probe_video, "run", new=AsyncMock(return_value=result)) as run, \
                        redirect_stdout(StringIO()) as out:
                    self.assertEqual(probe_video.main(args), 0)
                output = out.getvalue()
                if detailed:
                    self.assertEqual(json.loads(output)["full_video_url"], FULL)
                else:
                    self.assertEqual(len(output.splitlines()), 4)
                    self.assertIn("GIF / 动态预览（MP4）：https://cdn.example/preview.mp4", output)
                    self.assertNotIn("report_path", output)
                self.assertFalse(run.call_args.kwargs["verify"])
                self.assertFalse(run.call_args.args[3])  # 不保存媒体。
                self.assertTrue((Path(temp) / "probe" / "result.json").is_file())


if __name__ == "__main__":
    unittest.main()
