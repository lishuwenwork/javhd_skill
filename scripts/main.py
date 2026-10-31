"""入口：登录，或按视频 ID 同步到 Telegram 和夸克。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile

# 支持 python -I：只把自己编写的脚本目录加入搜索路径，不加入下载目录。
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (FORMAT_HINT, STATE, JobError, VideoRequest, detail_page_url, emit, load_config, login_status, parse_requests,
                    tags_with_id)
from records import UploadRecords, metadata_from_info, validate_config as validate_d1_config
from task_files import TaskFiles


@dataclass
class PreparedUpload:
    request: VideoRequest
    task: TaskFiles | None = None
    media: object = None
    error: Exception | None = None
    result: dict | None = None
    exit_code: int | None = None


async def settle(*awaitables):
    """等待两侧结束；中止时也不能在同步上传线程仍读文件时删除视频。"""
    group = asyncio.gather(*awaitables, return_exceptions=True)
    try:
        return await asyncio.shield(group)
    except asyncio.CancelledError:
        emit("stopping", message="正在等待已启动的任务结束，再按上传结果处理本地文件")
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


async def operate(config: dict, *, video_id: str = "", tags=None, login="all", force=False,
                  retained_task: TaskFiles | None = None, page_url="", prepared: PreparedUpload | None = None,
                  records=None) -> int:
    from source import prepare_media
    from telegram import Telegram
    from quark import Quark

    telegram = None
    quark = None
    statuses = {"telegram": "failed", "quark": "failed"}
    errors = {}
    work = None
    task = retained_task or (prepared.task if prepared is not None else None)
    cleanup_error = None
    own_records = records is None
    record_id = ""
    metadata_synced = False
    synced = {"telegram": False, "quark": False}
    result = {"id": video_id, "telegram": "failed", "quark": "failed", "cleaned": True, "db_recorded": False}
    if task is not None:
        work = task.directory
        statuses.update(task.data["statuses"])
        result.update({name + "_remote_ids": value for name, value in task.data["remote_ids"].items()})
        page_url = task.data.get("page_url", "")
        if "telegram_video_message_ids" in task.data:
            result["telegram_video_message_ids"] = list(task.data["telegram_video_message_ids"])

    async def sync_metadata():
        nonlocal metadata_synced
        try:
            await asyncio.to_thread(records.metadata, record_id, task.data.get("video_info", {}) if task else {})
        except Exception as exc:
            metadata_synced = False
            errors["database_metadata"] = safe_error(exc)
        else:
            metadata_synced = True
            errors.pop("database_metadata", None)

    async def sync_platform(name):
        if name == "telegram":
            if "telegram_video_message_ids" not in result:
                result["telegram_video_message_ids"] = list(result.get("telegram_remote_ids", []))[1:]
            remote_ids = result["telegram_video_message_ids"]
        else:
            remote_ids = result.get("quark_remote_ids", "")
        try:
            await asyncio.to_thread(records.platform, record_id, name, statuses[name] == "success", remote_ids)
        except Exception as exc:
            synced[name] = False
            errors["database_" + name] = safe_error(exc)
        else:
            synced[name] = True
            errors.pop("database_" + name, None)

    async def on_resolved(info):
        task.capture_info(info)
        task.save(statuses, result, state="running")
        await sync_metadata()
        if not metadata_synced:
            raise JobError("D1 元数据保存未确认，未继续下载；请检查网络后重试保留任务")

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
        if video_id:
            if records is None:
                records = UploadRecords(config)
            record_id = await asyncio.to_thread(
                records.begin, video_id, page_url or f"https://javhd.com/zh/id/{video_id}",
                record_id=task.data.get("d1_record_id", "") if task else "", retry=retained_task is not None,
            )
            result["record_id"] = record_id
            if task is not None:
                task.data["d1_record_id"] = record_id
        if prepared is not None and prepared.error is not None:
            raise prepared.error
        if any(status == "success" for status in statuses.values()):
            await sync_metadata()
            for name, status in statuses.items():
                if status == "success":
                    await sync_platform(name)
        names, tasks = [], []
        for name, connect in (("telegram", connect_telegram), ("quark", connect_quark)):
            if (video_id and statuses[name] != "success") or (not video_id and login in ("all", name)):
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

        if connected and all(isinstance(value, BaseException) for value in connected.values()) \
                and not any(status == "success" for status in statuses.values()):
            raise JobError("两个上传目标均未登录，未下载视频")
        if task is None:
            task = TaskFiles.create(ROOT, video_id, tags or [], config.get("telegram_channel_id"), page_url=page_url)
            task.data["d1_record_id"] = record_id
            work = task.directory
        if any(not isinstance(value, BaseException) for value in connected.values()):
            task.save(statuses, result, state="running")
            media = prepared.media if prepared is not None else task.restore_media()
            if media is None:
                emit("stage", id=video_id, stage="download")
                directory = work if retained_task is None else Path(tempfile.mkdtemp(prefix="prepare-", dir=work))
                media = await prepare_media(video_id, directory, config, on_resolved=on_resolved,
                                            **({"page_url": page_url} if page_url else {}))
                task.capture_media(media)
                task.save(statuses, result, state="running")
            elif hasattr(media, "info"):
                task.capture_info(media.info)
            await sync_metadata()
            if not metadata_synced:
                raise JobError("D1 元数据保存未确认，未开始上传；任务文件已保留")
            media.retain_work_files = True
            emit("stage", id=video_id, stage="upload")

            async def upload(name, action):
                if statuses[name] == "success":
                    return result[name + "_remote_ids"]
                if isinstance(connected.get(name), BaseException):
                    return connected[name]
                value = await action()
                statuses[name] = "success"
                result[name + "_remote_ids"] = value
                if name == "telegram":
                    confirmed = getattr(telegram, "video_message_ids", None)
                    result["telegram_video_message_ids"] = list(confirmed) if isinstance(confirmed, list) else list(value)[1:]
                # 先保存可恢复状态；D1 写入失败不会把真实远端成功改成失败或重新上传。
                task.save(statuses, result, state="running")
                await sync_platform(name)
                return value

            async def upload_quark():
                return await asyncio.to_thread(
                    quark.upload, media, checkpoint=task.data.get("quark_upload"),
                    checkpoint_callback=task.save_quark_checkpoint, retry=retained_task is not None,
                )

            publish_tags = tags_with_id(video_id, tags or [])
            uploads = await settle(
                upload("telegram", lambda: telegram.upload(media, publish_tags)),
                upload("quark", upload_quark),
            )
            for name, value in zip(("telegram", "quark"), uploads):
                if isinstance(value, BaseException):
                    errors[name] = safe_error(value)
    except Exception as exc:
        errors["job"] = safe_error(exc)
    finally:
        if video_id:
            partial = getattr(telegram, "video_message_ids", None) if telegram else None
            if isinstance(partial, list) and partial:
                result["telegram_video_message_ids"] = list(partial)
            if record_id:
                if not metadata_synced:
                    await sync_metadata()
                for name in statuses:
                    if not synced[name]:
                        await sync_platform(name)
            result["db_recorded"] = bool(record_id) and metadata_synced and all(synced.values())
            if task is not None:
                complete = all(status == "success" for status in statuses.values())
                try:
                    task.save(statuses, result, state="complete" if complete else "retained")
                    result["retry_ready"] = True
                except Exception as exc:
                    result["retry_ready"] = False
                    errors["task_manifest"] = safe_error(exc)
                if complete and result["db_recorded"]:
                    try:
                        await asyncio.to_thread(task.delete, ROOT)
                    except Exception as exc:
                        cleanup_error = safe_error(exc)
            result["cleaned"] = work is None or not work.exists()
            result["retained"] = work is not None and work.exists()
            result["awaiting_decision"] = result["retained"]
            if not result["retained"]:
                result["retry_ready"] = False
            if result["retained"]:
                result["retained_dir"] = str(work.resolve())
                result["next_actions"] = ["retry", "delete"] if result.get("retry_ready") else ["inspect", "delete"]
            result.update(statuses)
            result["errors"] = errors
            result["notification"] = notification(video_id, statuses)
            if cleanup_error and not result["cleaned"]:
                result["cleanup_error"] = cleanup_error
                result["notification"] += "（本地清理失败）"
            if result["retained"]:
                result["notification"] += "（任务文件已保留，等待选择删除或重试）"
            if "task_manifest" in errors and result["retained"]:
                result["notification"] += "（任务清单保存失败，重试前需核对上传结果）"
            if not result["db_recorded"]:
                result["notification"] += "（D1记录未确认）"
            result["saved_message"] = False
            if config["notify_saved_messages"] and telegram is not None:
                try:
                    await telegram.notify(result["notification"])
                    result["saved_message"] = True
                except Exception as exc:
                    result["notification_error"] = safe_error(exc)
            if prepared is not None:
                prepared.result = dict(result)
            emit("result", **result)
        for name, close in (("telegram", telegram.close if telegram else None),
                            ("quark", (lambda: asyncio.to_thread(quark.close)) if quark else None)):
            if close:
                try:
                    await close()
                except Exception as exc:
                    emit("close_error", service=name, error=safe_error(exc))
        if own_records and records is not None:
            await asyncio.to_thread(records.close)
    return int(any(status != "success" for status in statuses.values()) or not result["cleaned"] or not result["db_recorded"])


async def operate_batch(config: dict, requests: list[VideoRequest], *, force=False, records=None) -> int:
    """一个下载生产者、一个上传消费者；最多一条正在上传和一条下载中/待上传。"""
    from source import prepare_media

    own_records = records is None
    records = records or UploadRecords(config)
    try:
        await asyncio.to_thread(records.initialize)
    except BaseException:
        if own_records:
            await asyncio.to_thread(records.close)
        raise

    queue = asyncio.Queue(maxsize=1)
    capacity = asyncio.Semaphore(2)
    stopping = asyncio.Event()
    prepared_items = []

    async def produce():
        for request in requests:
            await capacity.acquire()  # 必须在解析链接、创建媒体目录前取得容量。
            if stopping.is_set():
                capacity.release()
                break
            item = PreparedUpload(request)
            prepared_items.append(item)
            try:
                item.task = TaskFiles.create(ROOT, request.video_id, request.tags,
                                             config.get("telegram_channel_id"), page_url=request.page_url)
                item.task.data["d1_record_id"] = await asyncio.to_thread(
                    records.begin, request.video_id, request.page_url or f"https://javhd.com/zh/id/{request.video_id}")
                item.task.save({"telegram": "failed", "quark": "failed"}, {}, state="running")
                emit("stage", id=request.video_id, stage="download")
                async def on_resolved(info):
                    item.task.capture_info(info)
                    item.task.save({"telegram": "failed", "quark": "failed"}, {}, state="running")
                    await asyncio.to_thread(records.metadata, item.task.data["d1_record_id"], metadata_from_info(info))
                item.media = await prepare_media(request.video_id, item.task.directory, config,
                                                on_resolved=on_resolved,
                                                **({"page_url": request.page_url} if request.page_url else {}))
                if item.media is None:
                    raise JobError("媒体准备没有返回可上传文件")
                item.task.capture_media(item.media)
                item.task.save({"telegram": "failed", "quark": "failed"}, {}, state="running")
            except asyncio.CancelledError:
                item.error = JobError("批量任务已中止，未开始上传；已有任务文件保留")
                raise
            except Exception as exc:
                item.error = exc
            await queue.put(item)
        await queue.put(None)

    async def finish(item):
        request = item.request
        try:
            item.exit_code = await operate(config, video_id=request.video_id, tags=request.tags,
                                           force=force, page_url=request.page_url, prepared=item, records=records)
        except Exception as exc:
            # 非预期收尾异常也不能让生产者堵在满队列，或把这一项当成成功。
            item.exit_code = 1
            item.result = item.result or {"id": request.video_id, "telegram": "failed", "quark": "failed"}
            item.result.setdefault("errors", {})["batch_item"] = safe_error(exc)
            emit("error", id=request.video_id, message=safe_error(exc))

    async def consume():
        while True:
            item = await queue.get()
            if item is None or stopping.is_set():
                return
            try:
                await finish(item)
            finally:
                capacity.release()

    producer = asyncio.create_task(produce())
    consumer = asyncio.create_task(consume())
    group = asyncio.gather(producer, consumer)
    interrupted = False
    try:
        await asyncio.shield(group)
    except asyncio.CancelledError:
        interrupted = True
        stopping.set()
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        emit("stopping", message="批量已停止后续下载，等待当前上传结束，保留尚未上传的任务文件")
        await consumer  # 同步夸克线程结束前不能关闭其原文件或启动另一条上传。
        await asyncio.gather(group, return_exceptions=True)
        for item in prepared_items:
            if item.exit_code is None:
                item.error = item.error or JobError("批量任务已中止，未开始上传；已有任务文件保留")
                await finish(item)

    indexed = {item.request.video_id: item for item in prepared_items}
    results = []
    for request in requests:
        item = indexed.get(request.video_id)
        if item is None:
            results.append({"id": request.video_id, "status": "skipped"})
        else:
            results.append({**(item.result or {"id": request.video_id}), "exit_code": item.exit_code})
    summary = {"total": len(requests), "interrupted": interrupted, "results": results}
    summary["success_count"] = sum(item.get("exit_code") == 0 for item in results)
    summary["skipped_count"] = sum(item.get("status") == "skipped" for item in results)
    summary["failed_count"] = len(results) - summary["success_count"] - summary["skipped_count"]
    emit("batch_result", **summary)
    if own_records:
        await asyncio.to_thread(records.close)
    return 130 if interrupted else int(bool(summary["failed_count"]))


def retained_action(args) -> int:
    """只有用户明确选择后才进入重试或删除；与普通上传共用运行锁。"""
    from filelock import FileLock, Timeout

    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with FileLock(str(STATE / "task.lock"), timeout=0):
            task = TaskFiles.load(ROOT, args.retry or args.delete, retry=bool(args.retry))
            if args.delete:
                task.delete(ROOT)
                emit("cleanup_result", id=task.data["id"], cleaned=True,
                     notification=f"id:{task.data['id']} 本地文件已删除")
                return 0
            config = load_config()
            channel = task.data.get("telegram_channel_id")
            if channel is not None and channel != config.get("telegram_channel_id"):
                raise JobError("当前频道配置与保留任务不一致；未重试，请先核对目标频道")
            return asyncio.run(operate(config, video_id=task.data["id"], tags=task.data["tags"], retained_task=task))
    except Timeout:
        emit("error", message="已有任务运行，未执行重试或删除")
        return 1
    except (KeyboardInterrupt, EOFError):
        emit("error", message="操作已中止，请先核对远端和保留任务目录")
        return 130
    except Exception as exc:
        emit("error", message=safe_error(exc))
        return 1


def doctor(config: dict) -> int:
    from common import check_crypto
    from telegram import check_telegram_proxy
    from quark import prepare_runtime
    from telegram_media import MAX_VIDEO_SIZE, PREMIUM_MAX_VIDEO_SIZE
    import imageio_ffmpeg
    from importlib.metadata import version

    proxy_status = check_telegram_proxy(config["telegram_proxy"])
    result = {**check_crypto(), **proxy_status, **prepare_runtime(),
              "ffmpeg": imageio_ffmpeg.get_ffmpeg_exe(),
              "telegram_credentials_ready": bool(config["telegram_api_id"] > 0 and config["telegram_api_hash"]),
              "telegram_channel_ready": config["telegram_channel_id"] != 0,
              "d1_config_ready": validate_d1_config(config), "snowflake_id_version": version("snowflake-id"),
              "telegram_premium": config.get("telegram_premium", False),
              "telegram_split_direction": config.get("telegram_split_direction", "tail"),
              "telegram_video_limit_bytes": PREMIUM_MAX_VIDEO_SIZE if config.get("telegram_premium", False) else MAX_VIDEO_SIZE}
    emit("check", **result)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help='原始指令，支持多 ID 或多行 "ID：123 URL：https://javhd.com/详情页"')
    parser.add_argument("--id", "--ids", nargs="+", action="extend", help="一个或多个视频 ID，按顺序处理")
    parser.add_argument("--url", help="单个 ID 的详情页 URL；批量不同 URL 请使用多行 --input")
    parser.add_argument("--tags", help='例如 "#4k、#高清"，仅用于 Telegram')
    parser.add_argument("--login", choices=["all", "telegram", "quark"])
    parser.add_argument("--force", action="store_true", help="重新初始化指定平台的登录；夸克会重新发起浏览器授权")
    parser.add_argument("--check", action="store_true", help="本地依赖自检；不登录、不发布")
    parser.add_argument("--init-db", action="store_true", help="通过 Cloudflare D1 REST API 初始化记录表（不上传）")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--retry", type=Path, metavar="TASK_DIR", help="用户选择后重试保留任务，只上传未成功端")
    recovery.add_argument("--delete", type=Path, metavar="TASK_DIR", help="用户选择后删除单个保留任务目录")
    args = parser.parse_args(argv)
    if args.retry or args.delete:
        if any((args.input is not None, args.id is not None, args.tags is not None, args.url is not None,
                args.login, args.force, args.check, args.init_db)):
            parser.error("--retry/--delete 不能与普通上传、登录或自检参数混用")
        return retained_action(args)
    requests = []
    if args.init_db and any((args.input is not None, args.id is not None, args.url is not None, args.tags is not None,
                            args.login, args.force, args.check)):
        parser.error("--init-db 不能与视频、登录、自检或重试参数混用")
    login, force = args.login or "all", args.force
    if args.input is not None:
        if args.id is not None or args.tags is not None or args.url is not None or args.login or args.check:
            print(FORMAT_HINT)
            return 2
        if args.input.strip() in {"电报重登", "夸克重登"}:
            login = "telegram" if args.input.strip() == "电报重登" else "quark"
            force = True
        else:
            try:
                requests = parse_requests(args.input)
            except ValueError:
                print(FORMAT_HINT)
                return 2
    elif args.id is not None or args.tags is not None or args.url is not None:
        try:
            if not args.id or args.login or args.check:
                raise ValueError(FORMAT_HINT)
            requests = parse_requests(f"id:{' '.join(args.id)} 标签:{args.tags or ''}")
            if args.url is not None:
                if len(requests) != 1:
                    raise ValueError(FORMAT_HINT)
                requests[0].page_url = detail_page_url(args.url)
        except ValueError:
            print(FORMAT_HINT)
            return 2

    def startup_failure(message, *, busy=False):
        results = []
        for request in requests:
            video_id = request.video_id
            result = {"id": video_id, "telegram": "failed", "quark": "failed",
                      "notification": f"id:{video_id} 上传失败" + ("（已有任务运行）" if busy else ""),
                      "cleaned": True, "saved_message": False, "errors": {"startup": message}}
            emit("result", **result)
            results.append({**result, "exit_code": 1})
        if len(requests) > 1:
            summary = {"total": len(requests), "interrupted": False, "results": results,
                       "success_count": 0, "failed_count": len(requests), "skipped_count": 0}
            emit("batch_result", **summary)
    started = False
    try:
        config = load_config()
        if args.check:
            return doctor(config)
        from filelock import FileLock, Timeout
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(STATE / "task.lock"), timeout=0):
                started = True
                if args.init_db:
                    records = UploadRecords(config)
                    try:
                        records.initialize()
                    finally:
                        records.close()
                    emit("database_ready", table="video_uploads", ready=True)
                    return 0
                if len(requests) > 1:
                    return asyncio.run(operate_batch(config, requests, force=force))
                request = requests[0] if requests else VideoRequest("", [])
                return asyncio.run(operate(config, video_id=request.video_id, tags=request.tags,
                                           page_url=request.page_url, login=login, force=force))
        except Timeout:
            if requests:
                startup_failure("已有任务运行", busy=True)
            else:
                emit("error", message="已有任务运行，请等待结束后再登录")
            return 1
    except (KeyboardInterrupt, EOFError):
        emit("error", message="任务已中止；再次上传前请先检查两个目标")
        return 130
    except Exception as exc:
        if requests and not started:
            startup_failure(safe_error(exc))
        else:
            emit("error", message=safe_error(exc))
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
