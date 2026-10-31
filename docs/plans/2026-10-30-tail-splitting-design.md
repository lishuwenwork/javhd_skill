# Telegram Split Direction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 可配置从开头或尾端规划电报视频分段；从尾端规划时余量留在首段，文件编号和播放顺序仍从头到尾。

**Architecture:** 新增 telegram_split_direction=head|tail；按用户进一步明确的要求，代码、旧配置缺省和分发模板均默认 tail，保留显式 head 选择。沿用 FFmpeg 音视频包扫描，tail 模式仅保存关键帧时间和累计字节数，从总字节数向前贪心选择切点，最后按时间升序交给 FFmpeg 生成分段。保留实际大小验证、失败清理、会员免切割、夸克完整文件及成功 ID 留档行为。

**Tech Stack:** Python 3.11+、bisect、现有 FFmpeg、Telethon、unittest。

---

### Task 1: 测试方向语义

**Files:** tests/test_telegram_split.py、tests/test_portability.py。

1. 模拟已知关键帧包，验证 head 切点为 [4,8]、tail 为 [3,7]，保证改变真实切点而非只倒置文件名。
2. 验证非均匀时间间隔仍按字节大小规划，以及方向枚举和旧配置默认值。
3. 用同一本地生成的带音频视频验证两种模式的体积、可播放性、编号和完整连续的压缩包序列。

### Task 2: 接入配置与上传

**Files:** scripts/common.py、scripts/telegram_media.py、scripts/telegram.py、scripts/main.py、scripts/package_skill.py、config.example.json、config.json。

1. 增加并严格校验方向字段，不改变其他配置值。
2. 将选择贯通到切点规划；tail 模式保存关键帧累计位置并从结尾反向选点，输出仍按时间升序。
3. 自检与分段事件显示方向；会员模式继续直接使用原视频。

### Task 3: 文档和验收

**Files:** README.md、SKILL.md、references/runtime.md、dist/javhd-skill.zip。

1. 用对照表说明两种模式、余量位置和编号顺序。
2. 跑全部离线测试，重新打包并核对配置模板和个人配置、历史、会话排除规则。
