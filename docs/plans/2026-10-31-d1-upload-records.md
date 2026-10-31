# D1 upload records Implementation Plan

**Goal:** 通过 Cloudflare 官方 REST API 存储视频元数据、两端状态及真实远端 ID，使用 snowflake-id 主键，替换上传历史与结果 JSON 文件。

**Architecture:** `scripts/records.py` 管理固定 Cloudflare API 端点、参数化 SQL、初始化及单字段更新。默认每个视频 ID 一条最新记录；本地仅保留登录态、锁和失败任务恢复清单，清单关联 D1 记录 ID 并暂存补写所需元数据。D1 故障不触发文件记录回退或媒体重新上传，业务成功状态不会被写库故障改成失败；未同步时保留任务用于补写。

**Tech Stack:** 现有 httpx、Cloudflare D1 REST API、SQLite STRICT schema、snowflake-id 1.0.2、unittest。

## 实现与验证

1. 新建 `schemas/d1.sql`（11 个用户字段，状态约束 0/1，视频 ID 唯一）；雪花 ID 用十进制 TEXT 保存及传输，避免 API 的 JS 大整数精度损失。
2. 新建 `scripts/records.py`：凭据格式验证、Bearer 请求、双层 success 校验、脱敏错误、幂等建表、开始/恢复任务、更新元数据、分别更新电报/夸克结果。使用 snowflake-id 并通过锁保护生成器，可选实例编号支持多安装。
3. 修改配置模板及加载器，添加 `ACCOUNT_ID`、`DATABASE_ID`、`API_TOKEN` 及可选 `SNOWFLAKE_INSTANCE_ID`；真实配置只补充空白缺失项，保留原值。新增 `--init-db`；`--check` 仅检查本地就绪，不联网建表。
4. 修改 source 记录原生英文标题，准备媒体时回调保存用户所需 URL/标题；清单恢复后可补写，绝不存完整视频临时下载 URL。Telegram 确认相册后分别暴露正片 ID，预览消息不写正片列，部分成功也保存已确认正片 ID，全部完成才 status=1。
5. 修改 main 单条及批量流程：开始前确认 D1 可用，各端确认立即保存临时恢复状态并分别写库，最后核对同步；两端成功且 D1 已确认才自动清理。补写失败不重传已成功端。移除 uploaded-ids/result/batch-result JSON 写入，改为 D1 + 本次进程事件；删除仅清理本地媒体，不删除云端记录。
6. 调整既有测试读取进程事件及模拟 D1；新增 REST 协议/SQLite schema/真实 Snowflake/并发状态更新/写入超时/凭据脱敏/正片 ID 过滤/只补写重试的离线测试。同步 README、SKILL、运行协议及 ZIP 清单。

尚无用户 D1 凭据，只执行模拟 HTTP 和本地 SQLite 测试，不声称已创建或连接真实 D1 数据库。旧 ID 历史仅包含 ID，不能补出完整元数据；不自动导入或删除它。
