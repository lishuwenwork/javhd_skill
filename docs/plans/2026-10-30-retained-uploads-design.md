# Retained Upload Tasks Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 只有两端上传确认成功才自动删除媒体；失败、未确认或取消后保留任务文件并告知用户，等待明确选择删除或重试。

**Architecture:** 任务目录改为显式创建和受控清理，电报分段归主任务所有。每个目录保存不含 Cookie、授权或签名链接的任务清单，保留媒体路径、必要元数据及两端成功记录。用户选择后通过 --retry TASK_DIR 复用媒体、只重试未成功端，或通过 --delete TASK_DIR 删除本次目录；两个入口共用运行锁。夸克重试仍为新上传，不宣称已经接入 CLI 的断点恢复。

**Tech Stack:** Python、现有 MediaFiles/VideoInfo、JSON、filelock、unittest。

---

### Task 1: 文件生命周期及用户决策入口

**Files:** scripts/task_files.py、scripts/main.py、scripts/telegram.py、scripts/telegram_media.py。

1. 将任务目录与电报分段交给主任务统一管理；所有失败路径保留媒体，只有两端成功或用户明确删除才清理。
2. 原子保存任务清单，记录已成功端的远端 ID；文件指纹和路径范围校验保护重试与删除。
3. 区分有意保留和清理失败，在结果提供 retained_dir、awaiting_decision，通知提示用户选择。
4. 增加 --retry 和 --delete，拒绝越界目录，复用已有媒体并跳过已成功端，不自动执行用户决策。

### Task 2: 回归测试

**Files:** tests/test_retained_tasks.py、tests/test_pipeline.py、tests/test_telegram_split.py。

1. 覆盖双端成功清理、一端/双端失败保留、预处理失败保留、重试不下载且跳过成功端。
2. 验证成功端通知失败不改变清理规则，任务未确认/路径越界/文件变化不能直接重试或删除。
3. 验证电报分段在任务失败后保留，用户删除不影响其他任务、登录态或成功 ID 历史。

### Task 3: 文档和打包

**Files:** README.md、SKILL.md、references/runtime.md、scripts/package_skill.py、dist/javhd-skill.zip。

1. 删除原来的失败也清理说明，加入失败保留、决策命令和重试范围说明。
2. 完成全部离线测试并重新生成分发包，排除媒体、任务清单、配置与会话。
