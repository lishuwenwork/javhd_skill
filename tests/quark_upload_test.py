"""手动夸克上传测试：官方 CLI 浏览器授权、复用登录态、自选文件/目录、实时进度与速度。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from filelock import FileLock, Timeout

from common import STATE, JobError
from quark import AuthRequired, Quark, UploadUnconfirmed, prepare_runtime


def human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    raise ValueError("文件大小无效")


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


class ConsoleProgress:
    """按发送字节显示进度；速度不包含哈希时间，重试流量计入速度但不累加进度。"""

    def __init__(self, total: int, *, stream=None, clock=None):
        self.total = total
        self.stream = sys.stdout if stream is None else stream
        self.clock = time.monotonic if clock is None else clock
        self.current = 0
        self.wire_bytes = 0
        self.started = None
        self.finished = None
        self.message = "准备上传..."
        self.instant = False
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.last_time = self.clock()
        self.last_bytes = 0
        self.line_width = 0

    def stage(self, percent, message):
        with self.lock:
            # 官方 CLI 的 percent 来自汇总字节数，但成功仍须看最终结果行。
            self.message = message
            if message.startswith("秒传完成"):
                self.instant = True

    def transfer(self, current: int, total: int):
        with self.lock:
            if self.started is None:
                self.started = self.clock()
                self.last_time = self.started
            self.current, previous = min(max(current, 0), total), self.current
            self.wire_bytes += max(0, self.current - previous)
            self.total = total
            self.finished = self.clock() if self.current == total else None

    @property
    def average_speed(self) -> float:
        if self.started is None:
            return 0.0
        end = self.clock() if self.finished is None else self.finished
        return self.wire_bytes / max(end - self.started, 0.001)

    def render(self):
        with self.lock:
            now = self.clock()
            speed = (self.wire_bytes - self.last_bytes) / max(now - self.last_time, 0.001)
            self.last_bytes, self.last_time = self.wire_bytes, now
            percent = 100 * self.current / self.total if self.total else 0
            filled = min(24, int(percent * 24 / 100))
            bar = "#" * filled + "-" * (24 - filled)
            if self.instant:
                line = f"[秒传完成，无需发送文件数据] | {self.message}"
            else:
                line = (f"[{bar}] {percent:6.2f}% | {human_size(self.current)} / {human_size(self.total)}"
                        f" | 速度 {human_size(speed)}/s | {self.message}")
            terminal = self.stream.isatty()
            padding = " " * max(0, self.line_width - len(line)) if terminal else ""
            print(("\r" if terminal else "") + line + padding,
                  end="" if terminal else "\n", file=self.stream, flush=True)
            self.line_width = len(line)

    def _refresh(self):
        while not self.stop.wait(0.5):
            self.render()

    def __enter__(self):
        self.render()
        self.thread = threading.Thread(target=self._refresh, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join()
        self.render()
        if self.stream.isatty():
            print(file=self.stream, flush=True)


class QuarkUploadTest:
    """单独运行的交互测试类，不读取 Telegram 配置，也不删除本地原文件。"""

    def __init__(self):
        self.quark = Quark()

    @staticmethod
    def ask_code(message: str) -> str:
        print(f"\n{message}\n如果浏览器没有自动完成授权，请打开提示中的授权链接，"
              "完成后把链接里的 code 参数值粘贴到这里。", flush=True)
        return unquote(input("夸克授权码（直接回车取消）："))

    @staticmethod
    def read_file() -> Path:
        while True:
            value = unquote(input("\n请输入本地文件完整路径（支持带引号，Ctrl+C 退出）："))
            if not value:
                print("文件路径不能为空。")
                continue
            try:
                path = Path(value).expanduser().resolve(strict=True)
                if not path.is_file():
                    print("该路径不是文件，请重新输入。")
                    continue
                if path.stat().st_size == 0:
                    print("暂不测试空文件，请重新选择一个非空文件。")
                    continue
                with path.open("rb"):
                    pass
                return path
            except (OSError, ValueError, RuntimeError):
                print("文件不存在、路径无效或没有读取权限，请重新输入。")

    def run(self, *, force_login=False):
        try:
            print("夸克网盘上传测试（仅上传到你的夸克，不触发 Telegram）\n"
                  "正在检查已保存的授权；首次使用或授权失效时会打开浏览器授权。", flush=True)
            self.quark.login(force=force_login, prompt=self.ask_code)
            print("夸克已授权；有效期内下次无需重新授权。")
            path = self.read_file()
            directory = unquote(input("请输入网盘上传目录（如 /上传测试/资料，回车用 /上传测试）："))
            directory = directory or "/上传测试"
            print(f"\n本地文件：{path}\n文件大小：{human_size(path.stat().st_size)}\n"
                  f"网盘目录：{directory}\n正在查找目录，不存在的层级会自动创建...", flush=True)
            folder = self.quark.folder(directory)
            print("开始上传。进度为实际发送字节；100% 后仍须等待合并和远端核验。", flush=True)
            started = time.monotonic()
            with ConsoleProgress(path.stat().st_size) as progress:
                fid = self.quark.upload_file(path, parent_folder_id=folder,
                                             progress_callback=progress.stage,
                                             transfer_callback=progress.transfer)
            status = "上传成功，官方已返回文件 ID。"
            if progress.instant:
                status = "秒传成功，官方已返回文件 ID（未发送文件数据）。"
            speed = (f"平均发送速度：{human_size(progress.average_speed)}/s" if progress.started is not None
                     else "本次未发送文件数据，不计算上传速度。")
            print(f"\n{status}\n文件：{path.name}\n目录：{directory}\n"
                  f"远端文件 ID：{fid}\n{speed}\n"
                  f"总耗时：{time.monotonic() - started:.1f} 秒\n"
                  "本地原文件已保留。", flush=True)
        finally:
            self.quark.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relogin", action="store_true", help="忽略已保存的授权，重新授权")
    args = parser.parse_args(argv)
    try:
        prepare_runtime()
        STATE.mkdir(parents=True, exist_ok=True)
        # 与主流程共用锁，避免两个进程同时授权或上传。
        with FileLock(str(STATE / "task.lock"), timeout=0):
            QuarkUploadTest().run(force_login=args.relogin)
        return 0
    except Timeout:
        print("已有任务正在运行，请结束该任务后再运行上传测试。", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n测试已取消。本地原文件不会删除；如已开始上传，请先检查网盘目标目录再重试。")
        return 130
    except UploadUnconfirmed as exc:
        print(f"\n上传结果未确认：{exc}\n这不表示远端文件一定不存在。"
              "请先核对网盘，不要直接重复上传。本地原文件不会删除。", file=sys.stderr)
        return 1
    except AuthRequired as exc:
        print(f"\n{exc}\n本次没有上传。完成授权后可重新运行本测试。", file=sys.stderr)
        return 1
    except Exception as exc:
        message = str(exc) if isinstance(exc, JobError) else type(exc).__name__
        print(f"\n测试失败：{message}\n请检查网络、授权状态和网盘空间；如已发送数据，"
              "请先核对目标目录，不要直接重复上传。本地原文件不会删除。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
