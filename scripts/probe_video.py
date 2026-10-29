"""真实 ID 解析测试：可检查 MP4 响应和时长，不上传、不整段下载视频。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from PIL import Image

from common import STATE, JobError, load_config, save_json
from source import (SITE, best_source, download, ffmpeg, full_player_endpoint, inspect_detail,
                    inspect_video_url, load_playback, prepare_cover, resolve_preview,
                    small_response, web_url, website_client)


def error_text(error: Exception) -> str:
    if isinstance(error, JobError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__  # 不回显可能包含 Cookie 或签名的异常请求。


async def probe(client: httpx.AsyncClient, video_id: str) -> dict:
    result = {"id": video_id, "page_url": f"{SITE}/zh/id/{video_id}", "title_zh": "",
              "cover_url": "", "gif_url": "", "preview_url": "", "preview_format": "",
              "video_url": "", "full_video_url": "", "expected_duration_seconds": 0,
              "video_verification": {"status": "not_checked"}, "checks": {}, "notes": {}, "playback": {}}
    keys = ("title_zh", "cover_url", "preview_url", "full_video_url")
    try:
        body, page = await small_response(client, result["page_url"])
        host = urlsplit(page).hostname or ""
        if not (host == "javhd.com" or host.endswith(".javhd.com")):
            raise JobError("详情页跳转到了其他站点")
        result["page_url"] = page
        data = inspect_detail(body.decode("utf-8", errors="replace"), page, video_id)
        result.update(title_zh=data["title"], cover_url=data["cover_url"], preview_url=data["preview_url"],
                      expected_duration_seconds=data["expected_duration"])
        result["playback"] = {"page_endpoint": data["player_endpoint"], "role": data["player_role"],
                              "page_is_trailer": data["is_trailer"], "source_count": 0}
        playback = {}
        if data["player_error"]:
            result["notes"]["full_video_url"] = data["player_error"]
        elif data["player_endpoint"]:
            result["playback"].update(endpoint=full_player_endpoint(data["player_endpoint"]), is_trailer=False)
            try:
                playback, endpoint = await load_playback(client, data["player_endpoint"], page)
                result["playback"]["endpoint"] = endpoint
                sources = playback.get("sources")
                result["playback"]["source_count"] = len(sources) if isinstance(sources, list) else 0
                result["cover_url"] = result["cover_url"] or web_url(playback.get("poster"), endpoint)
                result["video_url"] = best_source(playback, endpoint)
                result["full_video_url"] = result["video_url"]
                result["notes"]["full_video_url"] = "来自本次完整源接口的 sources[].src；未下载完整视频"
            except Exception as exc:
                result["playback"]["error"] = error_text(exc)
                result["notes"]["full_video_url"] = error_text(exc)
        result["preview_url"] = await resolve_preview(client, video_id, page, result["preview_url"], playback)
    except Exception as exc:
        result["notes"]["page"] = error_text(exc)

    preview_path = urlsplit(result["preview_url"]).path.lower()
    if result["preview_url"]:
        result["preview_format"] = Path(preview_path).suffix.lstrip(".") or "unknown"
    if preview_path.endswith(".gif"):
        result["gif_url"] = result["preview_url"]
    for key in keys:
        result["checks"][key] = "ok" if result[key] else "missing"
    if not result["title_zh"]:
        result["notes"]["title_zh"] = "没有解析到原生中文标题；没有自动翻译"
    if not result["cover_url"]:
        result["notes"]["cover_url"] = "当前视频没有可提取的封面"
    if result["preview_format"] == "mp4":
        result["notes"]["preview_url"] = "站点动态预览实际为 MP4，不是原生 GIF"
    elif not result["preview_url"]:
        result["notes"]["preview_url"] = "未取得当前视频的有效动态预览；未使用推荐视频"
    if not result["full_video_url"]:
        result["notes"].setdefault("full_video_url", "完整源接口没有提供可用 MP4 地址")
    result["success"] = all(value == "ok" for value in result["checks"].values())
    result["scope"] = "提取完整源 URL；CDN 短预览回退最多读取 64 字节；未下载完整视频"
    return result


async def verify_video(client: httpx.AsyncClient, result: dict) -> None:
    result["scope"] = "限量读取 MP4 文件头尾并核对时长；未整段下载或逐帧验证"
    try:
        if not result["video_url"]:
            raise JobError("没有可验证的 video_url")
        verification = await inspect_video_url(client, result["video_url"], result["page_url"])
        result["video_verification"] = verification
        expected, actual = result["expected_duration_seconds"], verification["duration_seconds"]
        verification["status"] = "mp4_confirmed"
        verification["page_duration_matches"] = (abs(actual - expected) <= max(2, expected * 0.02)
                                                 if expected and actual else None)
        result["notes"]["full_video_url"] = "已验证 MP4 响应；页面时长仅作参考，不据此清空完整源"
    except Exception as exc:
        result["video_verification"] = {"status": "error", "error": error_text(exc)}
        result["notes"]["full_video_url"] = error_text(exc)
        result["full_video_url"] = ""
        result["checks"]["full_video_url"] = "verification_error"
    result["success"] = all(value == "ok" for value in result["checks"].values())


async def save_assets(client: httpx.AsyncClient, result: dict, directory: Path) -> None:
    result["files"], result["asset_errors"] = {}, {}
    if result["cover_url"]:
        try:
            path = await download(client, result["cover_url"], directory / "cover" / "source.img",
                                  referer=result["page_url"], limit=20 * 1024 * 1024)
            cover, _ = await asyncio.to_thread(prepare_cover, path)
            result["files"]["cover"] = str(cover)
        except Exception as exc:
            result["asset_errors"]["cover"] = error_text(exc)
    if result["preview_url"]:
        try:
            is_gif = bool(result["gif_url"])
            path = directory / "preview" / ("source.gif" if is_gif else "source.bin")
            await download(client, result["preview_url"], path, referer=result["page_url"], limit=100 * 1024 * 1024)
            result["files"]["preview"] = str(path)
            if is_gif:
                with Image.open(path) as image:
                    if image.format != "GIF" or getattr(image, "n_frames", 1) < 2:
                        raise JobError("预览不是有效的 GIF 动态图")
                result["files"]["gif"] = str(path)
            else:
                gif = path.with_name("generated.gif")
                await ffmpeg("-i", path, "-t", "5", "-an", "-vf", "fps=8,scale=480:-2", gif)
                result["files"]["generated_gif"] = str(gif)
                # 生成文件不算“提取到了原生 GIF URL”。
        except Exception as exc:
            result["asset_errors"]["preview"] = error_text(exc)


async def run(video_id: str, output: Path, cookies_file: Path | None, assets: bool,
              verify=False, anonymous=False) -> dict:
    config = load_config()
    if anonymous:
        config["javhd_cookies_file"] = ""
    elif cookies_file:
        config["javhd_cookies_file"] = str(cookies_file.expanduser().resolve())
    async with website_client(config) as client:
        result = await probe(client, video_id)
        if verify:
            await verify_video(client, result)
        if assets:
            await save_assets(client, result, output)
    result["cookies_loaded"] = bool(config["javhd_cookies_file"])
    return result


def format_result(result: dict) -> str:
    preview_label = "GIF / 动态预览"
    if result.get("preview_format") == "mp4":
        preview_label += "（MP4）"
    fields = (("中文标题", "title_zh"), ("封面", "cover_url"),
              (preview_label, "preview_url"), ("完整视频链接", "full_video_url"))
    return "\n".join(f"{label}：{result.get(key) or '未获取'}" for label, key in fields)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", default="59152", help="默认测试真实 ID 59152")
    login = parser.add_mutually_exclusive_group()
    login.add_argument("--cookies-file", type=Path, help="可选：自己的 Netscape Cookie 文件")
    login.add_argument("--no-cookies", action="store_true", help="忽略本地 Cookie 配置，匿名测试")
    parser.add_argument("--verify-video", action="store_true", help="限量读取 MP4 头尾；页面时长只作参考")
    parser.add_argument("--json", action="store_true", help="输出详细 JSON；默认只显示标题、封面、动态预览和完整链接")
    parser.add_argument("--save-assets", action="store_true", help="另存封面和已有短预览；不下载完整视频")
    parser.add_argument("--output", type=Path, help="新的输出目录；必须不存在，避免覆盖旧报告")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]+", args.id) or int(args.id) <= 0:
        parser.error("--id 必须是正整数")
    output = args.output or STATE / "probes" / f"{args.id}-{uuid.uuid4().hex[:8]}"
    output = output.expanduser().resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(json.dumps({"error": error_text(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        result = asyncio.run(run(args.id, output, args.cookies_file, args.save_assets,
                                verify=args.verify_video, anonymous=args.no_cookies))
    except Exception as exc:
        result = {"id": args.id, "success": False, "error": error_text(exc)}
    result["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["report_path"] = str(output / "result.json")
    save_json(output / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_result(result))
    if not args.json and (result.get("error") or result.get("notes", {}).get("page")):
        print(result.get("error") or result["notes"]["page"], file=sys.stderr)
    return 0 if result["success"] and not result.get("asset_errors") else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
