"""使用本地生成的纯色视频，对真实 FFmpeg/Pillow/hachoir 做集成测试。"""

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import httpx
from PIL import Image

import source


class MediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_validate_cover_gif_and_album_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture.mp4"
            await source.ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=640x360:r=24",
                                "-t", "2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                "-movflags", "+faststart", fixture)
            video_bytes = fixture.read_bytes()
            image = BytesIO()
            Image.new("RGB", (800, 500), "blue").save(image, "PNG")
            page = '''<h1>本地测试中文标题</h1>
<meta property="og:image" content="https://cdn.example/cover.png">
<player-component content-path="/zh/player_api?videoId=7&amp;is_trailer=0"></player-component>
<script type="application/ld+json">{"@type":"VideoObject","duration":"PT20M"}</script>'''
            def handler(request):
                if request.url.path == "/zh/id/123":
                    return httpx.Response(200, text=page)
                if request.url.path == "/zh/player_api":
                    return httpx.Response(200, json={"sources": [{"res": 720, "src": "https://cdn.example/full.mp4"}]})
                if request.url.path == "/cover.png":
                    return httpx.Response(200, content=image.getvalue(), headers={"Content-Type": "image/png"})
                if request.url.path == "/full.mp4":
                    return httpx.Response(200, content=video_bytes, headers={"Content-Type": "video/mp4"})
                if request.url.path == "/share2/trailers/123/5sec_1000_n.mp4":
                    return httpx.Response(404)
                raise AssertionError(str(request.url))
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            work = root / "job"
            work.mkdir()
            config = {"http_proxy": "", "javhd_cookies_file": ""}
            with patch.object(source.httpx, "AsyncClient", return_value=client):
                media = await source.prepare_media("123", work, config)
            self.assertEqual(media.video.name, "本地测试中文标题.mp4")
            self.assertEqual(media.video.read_bytes(), video_bytes)
            self.assertEqual(media.video_meta["width"], 640)
            self.assertAlmostEqual(media.video_meta["duration"], 2, delta=0.15)
            self.assertEqual(media.info.expected_duration, 1200)  # 页面时长不否决实际完整源。
            self.assertEqual(media.preview_meta["width"], 480)
            self.assertGreater(media.preview_meta["duration"], 1)
            self.assertTrue((work / "preview/preview.gif").is_file())
            with Image.open(media.cover) as cover:
                self.assertEqual(cover.format, "JPEG")
            with Image.open(media.thumb) as thumb:
                self.assertEqual(thumb.format, "JPEG")
                self.assertLessEqual(max(thumb.size), 320)


if __name__ == "__main__":
    unittest.main()
