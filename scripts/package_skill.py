"""以白名单生成仅含空白配置模板的 Agent Skills ZIP；不包含个人配置、登录态或运行环境。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "javhd"
TEMPLATE_NAME = "config.example.json"
EMPTY_CONFIG_FIELDS = {
    "telegram_api_id": 0,
    "telegram_api_hash": "",
    "telegram_channel_id": 0,
    "telegram_proxy": "",
    "http_proxy": "",
    "javhd_cookies_file": "",
    "ACCOUNT_ID": "", "DATABASE_ID": "", "API_TOKEN": "",
}
FILES = (
    ".gitignore", "SKILL.md", "README.md", TEMPLATE_NAME, "requirements.txt",
    "references/runtime.md", "references/d1.md",
    "scripts/main.py", "scripts/common.py", "scripts/source.py", "scripts/telegram.py", "scripts/telegram_media.py",
    "scripts/task_files.py", "scripts/records.py", "schemas/d1.sql",
    "scripts/quark.py", "scripts/quark_proxy.cjs", "scripts/package_skill.py", "scripts/probe_video.py",
    "vendor/quarkclouddrive/scripts/quark-drive.cjs", "vendor/quarkclouddrive/scripts/hash-worker.cjs",
    "tests/quark_upload_test.py", "tests/quark_network_check.py",
    "tests/test_pipeline.py", "tests/test_login.py", "tests/test_media.py", "tests/test_portability.py",
    "tests/test_probe.py", "tests/test_quark_upload.py", "tests/test_quark_proxy.py", "tests/test_telegram_proxy.py",
    "tests/test_telegram_split.py",
    "tests/test_membership_history.py",
    "tests/test_retained_tasks.py",
    "tests/test_quark_resume.py",
    "tests/test_detail_url.py", "tests/test_batch.py",
    "tests/test_studio_preview.py",
    "tests/d1_testing.py", "tests/test_records.py",
)


def build_archive(root: Path, output: Path, *, overwrite=False) -> dict:
    root = root.resolve()
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("输出文件必须使用 .zip 后缀")
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"缺少文件或不允许的符号链接：{name}")
    template = json.loads((root / TEMPLATE_NAME).read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        raise ValueError(f"{TEMPLATE_NAME} 必须是 JSON 对象")
    expected_keys = set(EMPTY_CONFIG_FIELDS) | {"telegram_premium", "telegram_split_direction",
                                              "upload_concurrency", "notify_saved_messages", "SNOWFLAKE_INSTANCE_ID"}
    if set(template) != expected_keys:
        raise ValueError(f"{TEMPLATE_NAME} 必须包含全部支持的配置项，不能包含额外字段")
    if type(template["SNOWFLAKE_INSTANCE_ID"]) is not int or not 0 <= template["SNOWFLAKE_INSTANCE_ID"] <= 1023:
        raise ValueError(f"{TEMPLATE_NAME} 的 SNOWFLAKE_INSTANCE_ID 必须为 0 到 1023 的整数")
    for key, empty in EMPTY_CONFIG_FIELDS.items():
        if type(template[key]) is not type(empty) or template[key] != empty:
            raise ValueError(f"{TEMPLATE_NAME} 的 {key} 必须为空白默认值；个人配置请填写本地 config.json")
    concurrency = template["upload_concurrency"]
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ValueError(f"{TEMPLATE_NAME} 的 upload_concurrency 必须是 1 到 16 的整数")
    if type(template["notify_saved_messages"]) is not bool:
        raise ValueError(f"{TEMPLATE_NAME} 的 notify_saved_messages 必须是布尔值")
    if type(template["telegram_premium"]) is not bool:
        raise ValueError(f"{TEMPLATE_NAME} 的 telegram_premium 必须是布尔值")
    if template["telegram_split_direction"] not in ("head", "tail"):
        raise ValueError(f"{TEMPLATE_NAME} 的 telegram_split_direction 必须是 head 或 tail")
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    header = skill.split("---", 2)[1] if skill.startswith("---\n") else ""
    if not re.search(rf"(?m)^name: {SKILL_NAME}$", header):
        raise ValueError("SKILL.md 的 name 必须与分发目录 javhd 一致")
    if output.exists() and not overwrite:
        raise FileExistsError("分发包已存在；请另选 --output，或确认后使用 --force")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w" if overwrite else "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            archive.write(root / name, f"{SKILL_NAME}/{name}")
    return {"path": str(output), "files": len(FILES), "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "javhd-skill.zip")
    parser.add_argument("--force", action="store_true", help="确认覆盖已经存在的分发 ZIP")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_archive(ROOT, args.output, overwrite=args.force), ensure_ascii=False))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
