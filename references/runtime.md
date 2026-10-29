# 运行协议

本文件面向负责执行本 skill 的 agent。所有相对路径以 skill 根目录为基准，不依赖 agent 的当前工作目录。

## 入口与参数

使用本 skill 的虚拟环境解释器运行 `scripts/main.py`，始终带 `-I`。脚本会且只会把自己的 `scripts` 目录加入模块搜索路径。

| 参数 | 行为 |
| --- | --- |
| `--check` | 本地自检；不登录、不下载、不发布 |
| `--login all` | 同时初始化/复用两侧登录 |
| `--input "电报重登"` | 重新登录 Telegram |
| `--input "夸克重登"` | 重新登录夸克 |
| `--id 123 --tags "#4k、#高清"` | 执行一个视频任务 |
| `--input "id:123 标签:#4k、#高清"` | 接收原始指令；优先使用上面的独立参数形式 |

不要同时启动多个命令；同一安装目录的运行锁覆盖登录和视频任务。自检不获取锁，不启动任务。

## 配置

根目录的本地 `config.json` 是唯一运行配置文件；分发包仅携带空白的 `config.example.json`，不包含个人配置。首次安装仅在 `config.json` 不存在时复制模板，再在本地填写真实配置；升级时保留已有配置，不用模板覆盖。程序不会自动创建配置，也不直接读取模板；缺少本地配置时会提示复制模板。所有路径都从 skill 的实际位置计算。个人配置、登录会话、二维码和 Cookie 文件不打包；自己的 `config.json` 可单独私下迁移，迁移前先核对目标机器已有配置。

- `telegram_channel_id`：唯一的频道目标配置来源。`0` 表示尚未设置；上传前必须是 `-100` 开头的频道 ID，数字或数字字符串均可。
- `telegram_api_id`、`telegram_api_hash`：Telegram 应用凭据，扫码仍需提供。
- `telegram_proxy`：Telegram 代理，支持 socks5/socks4/http。
- `http_proxy`：仅 JAVHD 页面和媒体下载代理。
- `javhd_cookies_file`：用户自己的 Netscape Cookie 文件；相对路径以 skill 根目录为基准。解析使用页面内部 ID，以 `is_trailer=0` 查询完整源；接口拒绝访问、无源或仅返回预告片时失败，不退回预告片冒充完整视频。
- `upload_concurrency`：默认 6，范围 1–16；实际在途分块上限 10。
- `notify_saved_messages`：默认 true，另向 Telegram“收藏夹”发送 ID/状态通知，不向频道额外发布通知。

支持环境覆盖：`TG_API_ID`、`TG_API_HASH`、`TG_PROXY`、`HTTP_PROXY_URL`、`JAVHD_COOKIES_FILE`。**频道 ID 不从环境变量读取**，只能更改本地配置。

夸克上传调用内置的官方 CLI 1.0.20（`vendor/quarkclouddrive/scripts/quark-drive.cjs`），固定以 Claude Code 宿主标识（`CLAUDECODE=1`）启动，会话 ID 每次进程生成。启动前校验两个 vendor 文件的 SHA-256 并要求 Node.js 16+。vendor 内容和校验值保持不变；项目自有的 `scripts/quark_proxy.cjs` 复制到 `.state/quark-cli/scripts`，通过 Node `--require` 预加载，接管 SDK 显式创建的 HTTP/HTTPS Agent。不执行官方安装脚本或自动升级。

### 夸克代理路由（仅作用于夸克子进程）

按 `QUARK_PROXY` → `HTTPS_PROXY` → `ALL_PROXY` → `HTTP_PROXY` 的顺序选择第一个非空变量，支持对应小写名，同名大写优先。只支持 HTTP/HTTPS 代理 URL，认证信息从环境读取且必须正确 URL 编码；不支持的协议、错误格式直接报代理配置失败，不猜测或回退。禁止把真实代理地址、凭据或会话加入代码、配置示例、命令行、日志或分发包。

选择代理后，账号查询、OAuth 请求、目录操作、上传端点和合并请求全程使用同一条代理；不根据 `NO_PROXY` 让某些域名直连。未配置则直连，`QUARK_PROXY=direct` 可显式强制夸克直连。只在 CLI 子进程里传递代理，父进程环境不变，Telegram 逻辑和其代理配置不变。网盘目标仍从根目录 `0` 定位/创建 `JAVHD`，不因代理切换改变。

HTTPS 目标经 CONNECT 隧道；代理 URL 的协议表示代理监听协议，而不是目的站协议。普通 HTTP CONNECT 代理不能误配成 HTTPS 代理，否则可能得到 `EPROTO` / `SSL wrong version number`。代理自身和目的站 TLS 都验证证书；企业 CA 可由环境 `NODE_EXTRA_CA_CERTS` 提供，不关闭证书校验。浏览器授权页仍使用浏览器自身网络，不由 Node 适配器设置系统代理。

### 两种环境分别自检

- `<PYTHON> -I <SKILL_ROOT>/scripts/main.py --check`：本地文件、SHA-256、Node 和代理配置检查，不联网；返回 `quark_network_mode`、`quark_proxy_source`，不回显代理值。
- `<PYTHON> -I <SKILL_ROOT>/tests/quark_network_check.py --direct`：只对此次夸克自检强制直连。
- `<PYTHON> -I <SKILL_ROOT>/tests/quark_network_check.py --require-proxy`：必须从环境取到可用的代理配置，没配置就失败，不直连兜底。

后两项只探测官方 API 的 TLS/HTTP 可达性，不加载官方 CLI、不读授权、不创建目录或上传文件。HTTP 响应不代表业务成功；成功还会返回 `tls_verified=true`、`authorization_checked=false`，业务授权和非秒传上传须另验。**本地直连通过不等于 Muse VM 等代理环境通过**，必须在目标 VM 单独验证代理链路。

适配器把直连故障、代理故障、明确的 OAuth 未授权分别归类。`write EPROTO` 等网络错误优先判定为对应链路失败，不能包装成“授权未完成”或索要授权码。只有浏览器等待/打开失败才提供本地授权码补充入口。超时或上传未确认不自动整项重传。

官方 CLI 使用 OAuth/刷新令牌，与旧 quarkpan 的网页 Cookie 是两套体系。旧 `.state/quark` Cookie 不能给新 CLI 使用，也不能用复制 Cookie、会话或 API 密钥的方式修复代理问题。此路由不读取 `config.json` 中的 `http_proxy` 或 `telegram_proxy`。

## 事件与退出码

标准输出通常是 UTF-8 JSON Lines，每行一个事件。无效原始输入例外：只输出格式提示，退出码为 2。依赖库可能向标准错误输出诊断信息；不能把标准错误中的文本当最终结果。

主要事件：

| `event` | 含义 |
| --- | --- |
| `check` | 依赖及配置就绪状态 |
| `qr` | Telegram 新二维码，字段为 `service` 和 PNG 的绝对 `path` |
| `login` | 某侧状态更新，字段为 `service`、`status` |
| `login_result` | 本次指定登录目标的最终状态及安全错误信息 |
| `stage` | 视频任务阶段：`download` 或 `upload` |
| `transfer` | 单个文件上传的统计：字节数、耗时、KiB/s、并发数、平均确认延迟 |
| `result` | 视频任务的最终结果；以本次进程的这一事件为准 |
| `stopping` | 中止请求已收到，正在等已启动的任务结束后清理 |
| `error` / `close_error` | 启动/退出错误，不表示媒体已上传成功 |

退出码：`0` 为命令成功，`1` 为运行/配置/上传失败，`2` 为输入或 CLI 参数错误，`130` 为未被内部收尾流程接管的中止。`--check` 的 0 仅表示本地自检完成，还必须检查各项 `*_ready`，不代表已登录或有发布权限。

最终成功结果的必要字段示例：

```json
{
  "event": "result",
  "id": "123",
  "telegram": "success",
  "quark": "success",
  "cleaned": true,
  "saved_message": true,
  "notification": "id:123 上传成功"
}
```

此示例省略了可选的远端 ID 和错误字段。`saved_message=false` 只表示“收藏夹”通知未发送或已禁用，不会把已成功的上传改成失败；聊天仍需通知。反之，`saved_message=true` 也不代表上传成功，应检查两侧状态。

## 登录与任务生命周期

运行时目录：

```text
.state/
  qr/telegram.png
  login-telegram.json
  login-quark.json
  result-{id}.json
  telegram.session
  quark-cli/              官方 CLI 运行副本及本机授权
  task.lock
```

夸克授权不在这里：官方 CLI 把授权保存在其运行目录的上两级，即 `.state/quark-cli`，不进入 `vendor/`，也不打包。

登录状态包括 `connecting`、`scan`、`ready` 和 `failed`。错误处理可能将中间状态更新为 `failed`，应结合本次 `login_result.errors` 判断原因。

- 首次两侧登录同时发起；已有有效会话时不会重复授权。夸克只在只读账号查询明确返回未授权时启动浏览器授权。
- Telegram 的 `qr` 事件表示已经生成新的 PNG；优先按事件展示，使用文件等待时必须核对本次修改时间和 `scan` 状态。
- Telegram 大约每 30 秒刷新二维码；成功后删除对应 PNG，保留会话。夸克浏览器授权失败或超时后，只由本地用户把授权码粘贴回终端，一次性交给官方 CLI，不再重复自动启动授权。
- `电报重登` 退出本工具旧的 Telegram 会话；`夸克重登` 不主动撤销官方授权，只在本机授权无效时重新完成浏览器授权，不影响其他客户端登录。
- 必须在执行进程所在机器保存并展示 QR 文件；agent 仅能读文字时，应让用户在该机器上打开图片。
- 进程不存活，扫码或授权就不能完成。宿主不能持续运行进程时，转为本地终端操作。

## 完整性、清理与不确定结果

目前仅处理普通完整 MP4；HLS、无中文标题、仅预告片或完整源接口拒绝访问会失败。下载长度和容器检查用于确认文件下载完整；页面标注时长仅作参考，不用它否决完整源，也不能单凭文件时长证明内容一定完整。

动态预览可能原本就是短 MP4，也可能是 GIF；没有当前视频自己的预览时从同一视频生成。实际 Telegram 相册中预览是无声 MP4，不是原生 GIF 动画。中文文件名仅替换非法字符并限制过长名称，标签不进入夸克文件名。Telegram 说明固定带上 `#{视频ID}`（例如 `#123`，未填标签时仅此一项），其他标签按原文保留并去重；纯数字标签不再添加 `id` 前缀，继续按实际标签长度设置 `MessageEntityHashtag` 实体。

两侧结束后自动删除本次 `downloads/{id}-随机串/`。处理 Ctrl+C 时会等待已启动的上传；夸克会终止官方 CLI 子进程，因此可能延迟退出。强杀、断电、磁盘拒绝删除都不能保证清理；检查 `cleaned`，不要擅自删除其他任务或会话。

夸克侧只把官方 CLI 的最终结果 `code=0` 且返回文件 ID 视为成功；官方命令单次超时 30 分钟。超时、中断、无文件 ID 或本地文件在过程中变化时报告“未确认”，不自动重传，应先检查网盘。旧实现的 `.state/quark` Cookie 已不再使用，也不会被自动导入官方授权。

同一个 ID 再次执行会重新发布，不提供历史去重或跨进程断点续传。发布阶段超时或结果无法确认时，不自动重试整项任务，应先检查频道和网盘。被运行锁拒绝的调用，不得使用其他进程或旧文件的成功结果。
