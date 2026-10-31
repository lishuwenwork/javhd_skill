# Telegram Video Splitting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 电报上传超过普通账号单文件上限的视频时，自动按顺序上传可独立播放的分段；夸克始终上传原始完整视频。

**Architecture:** 新增电报专用媒体准备模块。通过 FFmpeg framecrc 流式读取压缩包大小和视频关键帧，按字节预算选择切点，再使用 segment muxer 无重新编码地生成 MP4。检查每段实际大小，必要时缩小预算重做；分段仅存在于本次任务临时目录，上传结束或失败后清理。电报相册支持 2 到 10 个条目，预览只在首批出现，最后一批避免只剩单条。保持已有标签规则、频道配置、分块流水线和夸克实现。

**Tech Stack:** Python 3.11+、已有 imageio-ffmpeg、Telethon、hachoir、unittest。

---

### Task 1: 验证媒体切割边界

**Files:** scripts/telegram_media.py、tests/test_telegram_split.py。

1. 用本地生成的视频和音频、较小的测试上限验证多段输出、文件编号、实际体积、可解码性及所有压缩包连续完整。
2. 覆盖未超限不切割、失败清理、取消时结束 FFmpeg、变码率和无法安全切割时明确失败。
3. 不读取或上传真实媒体；临时文件均位于测试目录。

### Task 2: 集成电报发布

**Files:** scripts/telegram.py、tests/test_telegram_split.py。

1. 用分段上下文替换原来的超限拒绝；普通大小视频维持原流程。
2. 每段保留封面、缩略图和标签，文件名为原标题加从 1 开始的括号编号。
3. 按顺序分批发布，核对每批 random_id 和 grouped_id；后续批次失败不能报告整体成功，也不自动整项重发。
4. 验证双端并行时夸克仍取得原文件，电报失败不取消夸克。

### Task 3: 文档、打包和验收

**Files:** README.md、SKILL.md、references/runtime.md、scripts/package_skill.py、dist/javhd-skill.zip。

1. 文档说明电报专用上限、编号分段、预览一次、分批相册及结果规则。
2. 将新模块和测试加入分发白名单，保留个人配置排除规则。
3. 运行全部离线测试，重新打包并核对源码、模板和运行产物排除情况。

**Sources:** https://core.telegram.org/api/files ; https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/framecrcenc.c
