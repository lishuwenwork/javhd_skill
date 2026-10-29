"""夸克直连/出口代理只读自检：不加载官方 CLI、不登录、不上传文件。"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import JobError
from quark import DirectConnectionError, ProxyConnectionError, check_network, proxy_settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--direct", action="store_true", help="仅本次夸克自检强制直连，不更改系统环境")
    mode.add_argument("--require-proxy", action="store_true", help="必须已通过环境配置 HTTP/HTTPS 代理，否则失败")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.direct:
        env["QUARK_PROXY"] = "direct"
    try:
        if args.require_proxy and not proxy_settings(env)[0]:
            raise ProxyConnectionError("未配置夸克代理：请通过运行环境设置 QUARK_PROXY 或标准代理变量")
        result = check_network(env=env)
        print(json.dumps({"event": "quark_network_check", "ok": True, **result}, ensure_ascii=False))
        return 0
    except JobError as exc:
        kind = ("proxy_failure" if isinstance(exc, ProxyConnectionError) else
                "direct_failure" if isinstance(exc, DirectConnectionError) else "runtime_failure")
        print(json.dumps({"event": "quark_network_check", "ok": False, "kind": kind,
                          "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
