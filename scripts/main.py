"""入口：登录，或按视频 ID 同步到 Telegram 和夸克。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import tempfile

# 支持 python -I：只把自己编写的脚本目录加入搜索路径，不加入下载目录。
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (FORMAT_HINT, STATE, JobError, emit, load_config, login_status, parse_input,
                    save_json, tags_with_id)


async def settle(*awaitables):
    """等待两侧结束；中止时也不能在同步上传线程仍读文件时删除视频。"""
    group = asyncio.gather(*awaitables, return_exceptions=True)
    try:
        return await asyncio.shield(group)
    except asyncio.CancelledError:
        emit("stopping", message="正在等待已启动的任务结束并清理本地文件")
        return await group


def safe_error(error) -> str:
    if isinstance(error, BaseExceptionGroup):
        return safe_error(error.exceptions[0])
    return str(error) if isinstance(error, JobError) else type(error).__name__


def notification(video_id: str, statuses: dict) -> str:
    if all(status == "success" for status in statuses.values()):
        return f"id:{video_id} 上传成功"
    labels = {"telegram": "电报", "quark": "夸克"}
    detail = "；".join(f"{labels[key]}：{'成功' if value == 'success' else '失败'}"
                      for key, value in statuses.items())
    return f"id:{video_id} 上传失败（{detail}）"


async def operate(config: dict, *, video_id: str = "", tags=None, login="all", force=False) -> int:
    from source import prepare_media
    from telegram import Telegram
    from quark import Quark

    telegram = None
    quark = None
    statuses = {"telegram": "failed", "quark": "failed"}
    errors = {}
    work = None
    result = {"id": video_id, "telegram": "failed", "quark": "failed", "cleaned": True}

    async def connect_telegram():
        nonlocal telegram
        telegram = Telegram(config)
        await telegram.login(force=force)

    async def connect_quark():
        nonlocal quark
        quark = Quark()
        # 本地终端运行时允许粘贴官方授权码；被 agent 调用时没有交互输入，浏览器失败就如实报错。
        prompt = None
        if sys.stdin.isatty():
            def prompt(message):
                print(f"\n{message}\n在浏览器完成授权后，把授权页 URL 中 code 参数的值粘贴到这里。", flush=True)
                return input("夸克授权码（直接回车取消）：")
        await asyncio.to_thread(quark.login, force, prompt=prompt)

    try:
        names, tasks = [], []
        for name, connect in (("telegram", connect_telegram), ("quark", connect_quark)):
            if video_id or login in ("all", name):
                names.append(name)
                tasks.append(connect())
        connected = dict(zip(names, await settle(*tasks)))
        for name, value in connected.items():
            if isinstance(value, BaseException):
                errors[name] = safe_error(value)
                login_status(name, "failed")
        if not video_id:
            emit("login_result", **{name: "failed" if isinstance(value, BaseException) else "ready"
                                    for name, value in connected.items()}, errors=errors)
            return int(bool(errors))

        if all(isinstance(value, BaseException) for value in connected.values()):
            raise JobError("两个上传目标均未登录，未下载视频")
        downloads = ROOT / "downloads"
        downloads.mkdir(exist_ok=True)
        # 每次任务及每个下载资源都使用新的空目录；失败也清理，不删除登录会话。
        with tempfile.TemporaryDirectory(prefix=f"{video_id}-", dir=downloads) as directory:
            work = Path(directory)
            emit("stage", id=video_id, stage="download")
            media = await prepare_media(video_id, work, config)
            emit("stage", id=video_id, stage="upload")

            async def upload(name, action):
                if isinstance(connected[name], BaseException):
                    return connected[name]
                return await action()

            async def upload_quark():
                return await asyncio.to_thread(quark.upload, media)

            publish_tags = tags_with_id(video_id, tags or [])
            uploads = await settle(
                upload("telegram", lambda: telegram.upload(media, publish_tags)),
                upload("quark", upload_quark),
            )
            for name, value in zip(("telegram", "quark"), uploads):
                if isinstance(value, BaseException):
                    errors[name] = safe_error(value)
                else:
                    statuses[name] = "success"
                    result[name + "_remote_ids"] = value
        # 退出 TemporaryDirectory 后才发“完成”通知。
    except Exception as exc:
        errors["job"] = safe_error(exc)
    finally:
        if video_id:
            result["cleaned"] = work is None or not work.exists()
            result.update(statuses)
            result["errors"] = errors
            result["notification"] = notification(video_id, statuses)
            if not result["cleaned"]:
                result["notification"] += "（本地清理失败）"
            result["saved_message"] = False
            if config["notify_saved_messages"] and telegram is not None:
                try:
                    await telegram.notify(result["notification"])
                    result["saved_message"] = True
                except Exception as exc:
                    result["notification_error"] = safe_error(exc)
            save_json(STATE / f"result-{video_id}.json", result)
            emit("result", **result)
        for name, close in (("telegram", telegram.close if telegram else None),
                            ("quark", (lambda: asyncio.to_thread(quark.close)) if quark else None)):
            if close:
                try:
                    await close()
                except Exception as exc:
                    emit("close_error", service=name, error=safe_error(exc))
    return int(any(status != "success" for status in statuses.values()) or not result["cleaned"])


def doctor(config: dict) -> int:
    from common import check_crypto
    from telegram import check_telegram_proxy
    from quark import prepare_runtime
    import imageio_ffmpeg

    proxy_status = check_telegram_proxy(config["telegram_proxy"])
    result = {**check_crypto(), **proxy_status, **prepare_runtime(),
              "ffmpeg": imageio_ffmpeg.get_ffmpeg_exe(),
              "telegram_credentials_ready": bool(config["telegram_api_id"] > 0 and config["telegram_api_hash"]),
              "telegram_channel_ready": config["telegram_channel_id"] != 0}
    emit("check", **result)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help='原始指令，例如 "id:123 标签:#4k、#高清"')
    parser.add_argument("--id", help="视频 ID，与 --tags 一起使用")
    parser.add_argument("--tags", help='例如 "#4k、#高清"，仅用于 Telegram')
    parser.add_argument("--login", choices=["all", "telegram", "quark"])
    parser.add_argument("--force", action="store_true", help="重新初始化指定平台的登录；夸克会重新发起浏览器授权")
    parser.add_argument("--check", action="store_true", help="本地依赖自检；不登录、不发布")
    args = parser.parse_args(argv)
    video_id, tags = "", []
    login, force = args.login or "all", args.force
    if args.input is not None:
        if args.id is not None or args.tags is not None or args.login or args.check:
            print(FORMAT_HINT)
            return 2
        if args.input.strip() in {"电报重登", "夸克重登"}:
            login = "telegram" if args.input.strip() == "电报重登" else "quark"
            force = True
        else:
            try:
                video_id, tags = parse_input(args.input)
            except ValueError:
                print(FORMAT_HINT)
                return 2
    elif args.id is not None or args.tags is not None:
        try:
            video_id, tags = parse_input(f"id:{args.id or ''} 标签:{args.tags or ''}")
        except ValueError:
            print(FORMAT_HINT)
            return 2
    try:
        config = load_config()
        if args.check:
            return doctor(config)
        from filelock import FileLock, Timeout
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(STATE / "task.lock"), timeout=0):
                return asyncio.run(operate(config, video_id=video_id, tags=tags, login=login, force=force))
        except Timeout:
            if video_id:
                text = f"id:{video_id} 上传失败（已有任务运行）"
                result = {"id": video_id, "telegram": "failed", "quark": "failed", "notification": text,
                          "cleaned": True, "saved_message": False, "errors": {"startup": "已有任务运行"}}
                save_json(STATE / f"result-{video_id}.json", result)
                emit("result", **result)
            else:
                emit("error", message="已有任务运行，请等待结束后再登录")
            return 1
    except (KeyboardInterrupt, EOFError):
        emit("error", message="任务已中止；再次上传前请先检查两个目标")
        return 130
    except Exception as exc:
        if video_id:
            result = {"id": video_id, "telegram": "failed", "quark": "failed",
                      "notification": f"id:{video_id} 上传失败", "errors": {"startup": safe_error(exc)}}
            save_json(STATE / f"result-{video_id}.json", result)
            emit("result", **result)
        else:
            emit("error", message=safe_error(exc))
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
