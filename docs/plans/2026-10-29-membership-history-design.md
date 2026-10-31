# Telegram Membership and Successful ID History Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 配置会员账号跳过电报切割，普通账号保持现有分段；任一平台确认完整上传成功即持久记录视频 ID。

**Architecture:** 增加布尔配置 telegram_premium，旧配置缺省 false；会员使用 8000 个 512 KiB 分块的兼容上限且不切割。上传包装器在平台返回成功时立即原子更新 .state/uploaded-ids.json，不等待另一端完成。记录使用文件锁去重，后续失败不删除历史。记录失败保留真实平台状态并在结果和通知中明确标记。

**Tech Stack:** Python、Telethon、已有 filelock、JSON、unittest。

---

### Task 1: 回归测试

**Files:** tests/test_membership_history.py、tests/test_portability.py、tests/test_telegram_split.py。

1. 验证新配置的布尔类型、旧配置默认值、分发模板和个人配置隔离。
2. 验证会员不调用 FFmpeg、底层分块允许会员文件、超过会员上限不切割且明确失败；普通账号分段不变。
3. 验证两端任一成功立即记录、并发写入去重、重复失败不丢历史、两端失败不新增记录、磁盘写入失败如实报告。

### Task 2: 实现配置与持久化

**Files:** config.example.json、config.json、scripts/common.py、scripts/main.py、scripts/telegram.py、scripts/telegram_media.py、scripts/package_skill.py。

1. 添加会员字段，保留已有本地配置值；不展示或改写账号凭据。
2. 贯通会员媒体准备和上传分块上限，避免只跳过切割但仍被普通账号上限拒绝。
3. 使用原子替换和文件锁保存 ID 列表，在各平台完成回调中记录，保持上传状态与本地记录状态独立。

### Task 3: 文档和验收

**Files:** README.md、SKILL.md、references/runtime.md、dist/javhd-skill.zip。

1. 明确会员开关、约 4GB 上限、成功 ID 记录位置与不自动跳过重复上传的规则。
2. 运行全部离线测试，重新生成分发包；确认新配置和测试入包，个人配置、历史记录及会话不入包。
