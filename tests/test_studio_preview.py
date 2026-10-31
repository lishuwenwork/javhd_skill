"""工作室公开 ID/播放器 ID/预告媒体目录不同的预览解析；不访问真实网站。"""

import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import JobError
from probe_video import probe
from source import player_mode_endpoint, resolve, resolve_preview

PAGE = "https://javhd.com/zh/studio/room/example/video/1789639070-example"
ENDPOINT = "https://javhd.com/zh/player_api?videoId=130782&keep=yes&is_trailer=1"
SHORT = "https://cdn.example/share2/trailers/86708/5sec_1000_n.mp4"
TRAILER = "https://cdn.example/share2/trailers/86708/1min_4000_n.mp4"
FULL = "https://cdn.example/content/full.mp4?signature=example"
MP4 = b"\x00\x00\x00\x20ftyp" + b"0" * 56
HTML = '''<h1>工作室中文标题</h1><meta property="og:image" content="https://cdn.example/cover.jpg">
<player-component role="GUEST" content-class="show_content_studio" content-id="130782"
 content-path="/zh/player_api?videoId=130782&amp;keep=yes&amp;is_trailer=1"></player-component>
<thumb-component link-content="/zh/studio/room/example/video/99999-other"
 video-preview="https://cdn.example/unrelated.mp4"></thumb-component>'''


class StudioPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, *, upload=False, trailer=None, trailer_status=200, short_status=206, source_status=206):
        requests = []
        def handler(request):
            requests.append(request)
            if str(request.url) == PAGE:
                return httpx.Response(200, text=HTML)
            if request.url.path == "/zh/player_api":
                self.assertEqual(request.url.params["videoId"], "130782")
                self.assertEqual(request.url.params["keep"], "yes")
                self.assertEqual(request.headers.get("X-Requested-With"), "XMLHttpRequest")
                if request.url.params["is_trailer"] == "0":
                    return httpx.Response(200, json={"id": 130782, "sources": [{"src": FULL, "res": 1080}]})
                self.assertEqual(request.url.params["is_trailer"], "1")
                return httpx.Response(trailer_status, json=trailer if trailer is not None else {
                    "id": 130782, "sources": [{"src": TRAILER, "res": 1080}]})
            if str(request.url) in (SHORT, TRAILER):
                self.assertEqual(request.headers["Range"], "bytes=0-63")
                self.assertEqual(request.headers["Referer"], PAGE)
                status = short_status if str(request.url) == SHORT else source_status
                return httpx.Response(status, stream=httpx.ByteStream(MP4 if status == 206 else b"<html>missing</html>"))
            self.fail("不得请求无关预览、按错误编号猜路径或下载完整视频")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await resolve(client, "1789639070", page_url=PAGE) if upload else await probe(
                client, "1789639070", page_url=PAGE)
        return result, requests

    async def test_studio_preview_follows_current_trailer_media_directory_not_public_or_player_id(self):
        result, requests = await self.collect()
        self.assertTrue(result["success"])
        self.assertEqual(result["preview_url"], SHORT)
        self.assertEqual(result["preview_format"], "mp4")
        self.assertEqual(result["gif_url"], "")
        self.assertEqual(result["full_video_url"], FULL)
        self.assertEqual([request.url.params.get("is_trailer") for request in requests
                          if request.url.path == "/zh/player_api"], ["0", "1"])
        self.assertEqual(len(requests), 4)
        self.assertFalse(any(str(request.url) == TRAILER for request in requests))

    async def test_upload_resolver_uses_same_short_preview(self):
        info, requests = await self.collect(upload=True)
        self.assertEqual(info.preview_url, SHORT)
        self.assertEqual(info.video_url, FULL)
        self.assertEqual(info.video_id, "1789639070")
        self.assertEqual(len(requests), 4)

    async def test_supplied_trailer_is_validated_when_five_second_preview_is_missing(self):
        result, requests = await self.collect(short_status=404)
        self.assertEqual(result["preview_url"], TRAILER)
        self.assertEqual(result["full_video_url"], FULL)
        self.assertEqual(len(requests), 5)

    async def test_missing_or_html_previews_leave_full_source_intact(self):
        result, _ = await self.collect(short_status=404, source_status=200)
        self.assertEqual(result["preview_url"], "")
        self.assertEqual(result["checks"]["preview_url"], "missing")
        self.assertEqual(result["full_video_url"], FULL)
        self.assertEqual(result["checks"]["full_video_url"], "ok")

    async def test_trailer_api_failure_does_not_clear_full_source(self):
        for status, data in ((403, {}), (200, []), (200, {"sources": None}),
                             (200, {"sources": [{"src": FULL}]}),
                             (200, {"sources": [{"src": "https://[invalid"}, None]})):
            with self.subTest(status=status, data=data):
                result, requests = await self.collect(trailer_status=status, trailer=data)
                self.assertEqual(result["preview_url"], "")
                self.assertEqual(result["full_video_url"], FULL)
                self.assertEqual(len(requests), 3)

    async def test_trailer_cannot_become_full_video_source(self):
        def handler(request):
            if str(request.url) == PAGE:
                return httpx.Response(200, text=HTML)
            return httpx.Response(200, json={"sources": [{"src": TRAILER}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(JobError):
                await resolve(client, "1789639070", page_url=PAGE)

    async def test_fallback_candidates_are_bounded_and_deduplicated(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path == "/zh/player_api":
                return httpx.Response(200, json={"sources": [{"src": TRAILER}] * 20})
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await resolve_preview(client, "1789639070", PAGE, player_endpoint=ENDPOINT)
        self.assertEqual(result, "")
        self.assertEqual(len(seen), 3)  # 一个接口、一个五秒候选、一个接口预告。

    async def test_original_preview_avoids_all_extra_requests(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: self.fail("已有当前预览不应再次探测"))) as client:
            self.assertEqual(await resolve_preview(client, "1789639070", PAGE, "https://cdn.example/current.gif",
                                                   player_endpoint=ENDPOINT), "https://cdn.example/current.gif")

    async def test_cross_origin_or_invalid_player_endpoint_is_never_requested(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: self.fail("不得访问未经验证的播放器接口"))) as client:
            for endpoint in ("https://other.example/zh/player_api", "https://[invalid", "https://javhd.com/other"):
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(await resolve_preview(client, "1789639070", PAGE,
                                                           player_endpoint=endpoint), "")

    async def test_malformed_trailer_json_is_nonfatal(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"not-json"))) as client:
            self.assertEqual(await resolve_preview(client, "1789639070", PAGE, player_endpoint=ENDPOINT), "")

    def test_mode_switch_preserves_internal_id_route_query_and_removes_duplicate_mode(self):
        endpoint = "https://javhd.com/zh/player/130782?videoId=130782&token=example&is_trailer=0&is_trailer=0"
        self.assertEqual(player_mode_endpoint(endpoint, trailer=True),
                         "https://javhd.com/zh/player/130782?videoId=130782&token=example&is_trailer=1")


if __name__ == "__main__":
    unittest.main()
