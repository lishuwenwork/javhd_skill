# Config Template Distribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 分发包只携带空白配置模板，保留现有个人配置，并在 README 声明安装、升级和迁移规则。

**Architecture:** 保持运行时读取本地 config.json；分发白名单改用 config.example.json，打包前检查模板中个人字段为空。配置缺失时给出复制模板的提示，不自动创建或覆盖配置。

**Tech Stack:** Python 3.11+ 标准库、unittest、Markdown、ZIP。

---

### Task 1: 固定分发与配置边界

**Files:** scripts/common.py、scripts/package_skill.py、config.example.json、tests/test_portability.py、tests/test_quark_proxy.py。

1. 保存本地 config.json 校验值，仅用于验证文件未变，不输出凭据。
2. 更新测试：个人配置不入包、模板不得含凭据或代理、缺少本地配置时提示初始化、解压后复制模板仍支持跨工作目录自检。
3. 运行相关离线测试，确认旧实现不满足新的分发要求。
4. 新增模板，更新分发白名单和模板校验，以及缺失配置提示。

### Task 2: 同步使用说明

**Files:** README.md、SKILL.md、references/runtime.md。

1. README 明确模板与个人配置的区别，并提供不会覆盖已有配置的首次复制方法。
2. 更新安装、打包、升级和私有迁移说明；运行文档与入口使用相同规则。

### Task 3: 验收并生成新分发包

**Files:** dist/javhd-skill.zip。

1. 运行全部 test_*.py 离线测试，记录通过和跳过情况。
2. 重新生成现有 ZIP，核对模板、相对链接和白名单；确认无 config.json、会话、虚拟环境或下载文件。
3. 对比本地配置校验值，确认原文件字节未变；不执行真实登录、下载或上传。
