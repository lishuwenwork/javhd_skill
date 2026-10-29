# JAVHD 通用 Agent Skill

按视频 ID 下载 JAVHD 完整视频，并行上传到本地配置的 Telegram 频道和夸克 `JAVHD` 文件夹。Telegram 以“无声动态预览 + 完整视频”组成相册，设置中文标题、封面及可点击标签；夸克文件名使用中文标题。

本项目按 [Agent Skills 规范](https://agentskills.io/specification)组织，只有一个自包含 skill，**不创建任何宿主专用的 skill 入口，不依赖特定模型、工具名称或绝对安装路径**。

## 目录结构

```text
javhd/                         分发包中的标准安装目录
├── SKILL.md                   标准元数据、触发条件及操作指令
├── README.md                  安装、配置、打包和迁移
├── config.example.json        空白配置模板，随包分发
├── requirements.txt
├── scripts/
│   ├── main.py                统一 CLI 入口
│   ├── common.py              输入、配置、二维码及自检
│   ├── source.py              解析、下载、封面及动态预览
│   ├── telegram.py            扫码、分块管线及相册
│   ├── quark.py               官方 CLI 适配：授权、目录、上传
│   ├── quark_proxy.cjs        夸克子进程 HTTP/HTTPS 代理预加载器
│   ├── probe_video.py         真实 ID 解析测试，不上传
│   └── package_skill.py       只打包允许分发的文件
├── vendor/
│   └── quarkclouddrive/       固定的官方夸克网盘 CLI 1.0.20
├── references/
│   └── runtime.md             agent 执行协议和状态说明
└── tests/
    ├── quark_upload_test.py   手动测试夸克上传（控制台运行）
    ├── quark_network_check.py 只读直连/代理 TLS 网络自检
    └── test_*.py              离线测试
```

本地开发目录仍叫 `javhd_skill`，保留现有 `config.json` 和 `.venv`；生成 ZIP 时，顶层目录自动规范化为 `javhd/`，与 `SKILL.md` 中的 `name: javhd` 一致。安装时保留这个目录名。

## 1. 配置频道和账号

**配置分发声明：分发包只携带空白的 `config.example.json`；个人配置 `config.json`、登录会话、二维码和 Cookie 文件均不进入 ZIP。** 模板中的凭据、频道、代理和 Cookie 路径必须保持空白默认值，真实配置只在本地填写。

| 文件或目录 | 用途 | 随包分发 |
| --- | --- | --- |
| `config.example.json` | 空白配置模板，供首次安装复制 | 是 |
| `config.json` | 用户本地配置，包含自己的凭据、频道和代理设置 | 否 |
| `.state/` | 登录会话、二维码和运行状态 | 否 |

首次安装时，在 skill 根目录**仅在 `config.json` 不存在时**复制模板，再用本地编辑器填写自己的配置。程序只读取 `config.json`，不会自动用模板替代个人配置，也不会在配置缺失时创建文件。

Windows PowerShell：

```powershell
if (-not (Test-Path -LiteralPath config.json)) {
    Copy-Item -LiteralPath config.example.json -Destination config.json
}
```

Linux / macOS：

```sh
if [ ! -e config.json ]; then
    cp config.example.json config.json
fi
```

**升级时保留已有 `config.json`，不要用模板覆盖，也不要把个人配置写回模板。** 模板内容如下：

```json
{
  "telegram_api_id": 0,
  "telegram_api_hash": "",
  "telegram_channel_id": 0,
  "telegram_proxy": "",
  "http_proxy": "",
  "javhd_cookies_file": "",
  "upload_concurrency": 6,
  "notify_saved_messages": true
}
```

必须在本地填写：

- `telegram_api_id`、`telegram_api_hash`：来自 https://my.telegram.org 的应用凭据；扫码登录也需要这两项。
- **`telegram_channel_id`：要发布到的 `-100…` 频道 ID。** 只从本地此配置项读取，没有代码中的固定频道，也没有环境变量覆盖。`0` 表示尚未配置；分发模板始终使用 `0`，不会携带个人目标频道。

其他配置：

- `telegram_proxy`：Telegram 的 socks5/socks4/http 代理，可留空。
- `http_proxy`：JAVHD 页面及下载代理，可留空。
- `javhd_cookies_file`：用户自己有完整观看权限的 Netscape Cookie 文件；相对路径以 skill 根目录为基准。
- `upload_concurrency`：Telegram 分块并发数，默认 6，允许 1–16；实际同时在途的分块最多 10 个（协议上限保护）。
- `notify_saved_messages`：默认额外给 Telegram“收藏夹”发送一条 ID/状态通知，不向频道额外发通知。

API Hash、Cookie、会话、密码不要贴进聊天，也不要写入模板或提交到仓库。`config.json` 已被 `.gitignore` 忽略，打包脚本还通过独立白名单将其排除；不能只依赖 Git 忽略规则来保护 ZIP。环境覆盖及代理详细规则见 [运行协议](references/runtime.md)。

## 2. 安装 Python 依赖

需要 Python 3.11+、**Node.js 16+**（夸克使用官方 CLI）、可加载的 OpenSSL `libcrypto`、访问目标服务的网络，以及可写的安装目录。Windows 本机已在 Python 3.12 与 Node.js 22 验证；其他系统必须在目标环境运行自检，不能直接复制 Windows 虚拟环境。

以下命令都在**解压得到的 `javhd` 目录**执行。本地开发时则在 `javhd_skill` 目录执行。

### Windows PowerShell

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 按第 1 节复制模板并编辑本地 config.json 后自检；不登录、不发布
& .\.venv\Scripts\python.exe -I scripts/main.py --check
```

### Linux / macOS

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 按第 1 节复制模板并编辑本地 config.json 后自检
.venv/bin/python -I scripts/main.py --check
```

夸克的哈希、分片和合并由官方 CLI/SDK 负责，不再依赖旧 quarkpan 的原生 SHA1 适配。FFmpeg 由 `imageio-ffmpeg` 依赖提供。

自检要求 `native_enabled=true`、`quark_cli_version=1.0.20`，并返回可用的 Node.js 版本。实际上传前还必须确认 `telegram_credentials_ready=true` 和 `telegram_channel_ready=true`。自检成功不等于线上登录或发布权限已验证。

### Telegram 异步代理依赖与验证

Telethon 1.45.0 的异步代理需要 `python-socks[asyncio]`，只有 `PySocks` 不够。依赖清单加入 `python-socks[asyncio]>=2.7.2,<3`，支持 Python 3.11/3.12；请始终用**运行本 skill 的同一个虚拟环境解释器**安装完整依赖，并在安装后重启 Python 进程。Telethon 在导入时缓存代理后端，运行中装包但不重启仍可能无法启用代理。

MTProto 直连被拦截时，通过本机配置的 `telegram_proxy` 或由运行器安全注入 `TG_PROXY` 指定代理；非空 `TG_PROXY` 始终覆盖 `telegram_proxy`，不写回配置。支持 SOCKS5、SOCKS4 和 HTTP 代理，须有有效端口，用户名/密码中的特殊字符须 URL 编码。不要把真实代理地址、凭据、API 凭据或频道 ID 写入代码、文档示例或聊天，也不要把含密码的命令留在 shell 历史。推荐用运行器的环境/机密配置注入 `TG_PROXY`。

`TG_PROXY` 只用于 Telegram；`HTTP_PROXY` / `HTTPS_PROXY` / `QUARK_PROXY` 不能代替它。本次检查不修改上传流程、夸克逻辑或任何目标频道。配置了 Telegram 代理但异步依赖缺失或 Telethon 尚未加载时，客户端创建前和 `--check` 都会直接报出缺少 `python-socks` 的错误，**不会警告后继续直连**，也不靠屏蔽警告解决问题。

```powershell
# 安装/更新到当前依赖清单，然后重启运行中的 Python 进程
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip check

# 先在运行器中安全配置 TG_PROXY，或在本机配置 telegram_proxy；不在此展示代理值
& .\.venv\Scripts\python.exe -I scripts/main.py --check

# 真实连通性验收只登录 Telegram，不下载或发布视频
& .\.venv\Scripts\python.exe -I scripts/main.py --login telegram
```

Linux/macOS 对应使用 `.venv/bin/python` 执行上述 `-m pip ...`、`-I scripts/main.py --check` 与 `--login telegram`。

有效代理配置下，自检必须返回 `telegram_proxy_enabled=true`、`telegram_proxy_ready=true`、`telegram_proxy_backend=python-socks`，`telegram_proxy_type` 为实际协议；不会显示代理地址或凭据。`telegram_proxy_enabled=false` 表示本次没有启用 Telegram 代理，在直连被拦截的环境应先修正配置/环境继承。

`--check` 只证明配置和异步后端就绪，不证明出口可达。随后登录不应再出现 `proxy argument will be ignored because python-socks is not installed`；二维码生成或确认已登录后，才算实际连接验收通过。若仍有连接重置，应检查代理出口和 MTProto 支持，不要直接更换频道、凭据或反复重登。

### 夸克直连与出口代理

官方 CLI 1.0.20 的代理检测被置空，仅传入标准代理环境变量不会生效。本项目不修改 vendor 或跳过其 SHA-256 校验，而是在 CLI 启动前预加载项目自有的 Node HTTP/HTTPS 代理适配器。它覆盖 SDK 显式创建的 Agent，账号查询、授权接口、创建目录、分片上传和完成上传均采用同一条路由；HTTPS 使用 CONNECT 并保留证书校验。

代理只从运行环境读取，按下列优先级选择第一个非空值：

1. `QUARK_PROXY`
2. `HTTPS_PROXY`
3. `ALL_PROXY`
4. `HTTP_PROXY`

支持对应的小写变量，同名大写优先。仅支持 HTTP/HTTPS 代理 URL，可携带经 URL 编码的代理认证信息；SOCKS 等其他协议会明确报配置错误，不会悄悄直连。代理 URL 的协议必须对应代理自身的监听协议：HTTPS 目的站也常通过 HTTP CONNECT 代理访问，不能因此把代理协议误设成 HTTPS，否则可能出现 `EPROTO` / `SSL wrong version number`。

由 Muse VM 或启动器通过安全环境注入代理；**不把真实代理地址、账号密码写进代码、配置、命令示例、日志或分发包，也不要贴进聊天**。适配器只通过 CLI 子进程环境传递代理，不放入命令行，不改父进程环境，因此不改变 Telegram 代理逻辑，也不改变根目录 `JAVHD` 目标。`config.json` 的 `telegram_proxy`、`http_proxy` 分别仍只用于 Telegram 和 JAVHD 源站。

- 未配置代理时直连；`QUARK_PROXY=direct` 可仅对夸克显式强制直连，优先于其他代理变量。
- 选择代理后，CLI 的所有 HTTP/HTTPS 请求都走所选代理，包括不同域名的上传端点；本适配器不采用 `NO_PROXY` 分流。代理失败不回退直连，也不自动重新授权。
- 不关闭 TLS 校验；出口存在受信任的 TLS 中间证书时，使用运行环境的 `NODE_EXTRA_CA_CERTS` 提供 CA，不用 `NODE_TLS_REJECT_UNAUTHORIZED=0`。
- 浏览器打开的授权页由浏览器自己的网络设置控制；Node 适配器只控制 CLI 请求，不修改浏览器或系统代理。

先运行原有 `--check`：它只校验本地文件、Node 和代理配置，新增 `quark_network_mode`、`quark_proxy_source`，仅显示模式和来源变量名，不显示代理值。然后在**实际目标环境**分别做只读网络自检：

```powershell
# 本地依赖/代理配置检查；不联网
& .\.venv\Scripts\python.exe -I scripts/main.py --check

# 仅此次自检强制直连，不改父进程环境
& .\.venv\Scripts\python.exe -I tests/quark_network_check.py --direct

# 运行环境须已注入出口代理；未配置则失败，不回退直连
& .\.venv\Scripts\python.exe -I tests/quark_network_check.py --require-proxy
```

Linux/macOS 对应使用 `.venv/bin/python -I tests/quark_network_check.py --direct` 和 `.venv/bin/python -I tests/quark_network_check.py --require-proxy`。

网络自检不加载官方 CLI、不读取授权、不创建目录或上传文件。它仅检查到官方 API 的 TLS/HTTP 路径，成功返回 `tls_verified=true` 和 HTTP 状态；收到 HTTP 响应不等于业务成功或已授权，仍须核对状态码并另验授权/上传。退出码 `1` 会区分 `direct_failure`、`proxy_failure` 或 `runtime_failure`。直连失败、代理失败不能通过反复扫码解决。

**本地直连通过不等于 Muse VM 等代理环境通过。** 必须在 VM 里完成代理网络自检，再验证官方授权及非秒传文件上传。新 CLI 使用 OAuth 与刷新令牌；旧 quarkpan 的 `.state/quark` 网页 Cookie 是另一套登录体系，不能直接导入或给新 CLI 使用。代理问题不应通过复制 Cookie、会话或 API 密钥来解决。

## 3. 在其他 agent 中使用

### 宿主支持 Agent Skills

1. 解压 ZIP，得到完整的 `javhd/` 目录。
2. 将此目录放进**目标 agent 文档指定的 skill 搜索目录**，或通过其原生安装功能注册。不要只复制 `SKILL.md`。
3. 在这个安装目录重新创建虚拟环境、安装依赖；首次安装按第 1 节复制模板并填写本地 `config.json`，升级时保留已有配置。
4. 按宿主要求刷新 skill 列表或重启会话。
5. 启用名为 `javhd` 的 skill。是否支持 `/javhd` 之类的命令由宿主决定，不是本项目约定。

输入仍为：

```text
id:123 标签:#4k、#高清
id:123
```

第二行不填标签，Telegram 只使用默认的 `#123`。上传时**始终**附加 `#{视频ID}`，用户填写的标签紧随其后并去重；纯数字标签如 `#123` 保持原样，不添加 `id` 前缀。例如 `id:123 标签:#4k、#高清` 对应 `#123 #4k #高清`。

首次启用不带视频任务时初始化两侧登录：Telegram 显示二维码，夸克使用官方 CLI 的浏览器授权。重登输入：

```text
电报重登
夸克重登
```

### 宿主不支持 Skills，但能读文件、执行进程

不需要安装厂商插件。在 agent 的任务说明中提供类似指令：

> 读取 `/实际路径/javhd/SKILL.md`，将其所在目录作为 skill 根目录，按其中规则处理本次输入。使用该目录下的虚拟环境执行脚本，不假定当前工作目录或特定工具名称。本次输入：id:123 标签:#4k、#高清。

路径按目标机器实际位置替换。agent 还需要展示二维码 PNG、能打开浏览器或接收授权码，并能让登录和上传进程持续运行；缺少能力时转为用户本地终端操作。

### 只有聊天能力、没有执行环境

仅上传 ZIP 或粘贴 `SKILL.md` 不能让模型获得 Python、文件系统、网络或后台进程。需要在它能访问的机器/容器中安装本 skill，并提供命令执行及文件展示能力；否则只能由用户直接运行 CLI。

**ZIP 是分发容器，不是所有 agent 都能直接导入的通用安装协议。** 如果目标产品有专用导入格式或安装命令，应按其文档注册解压后的目录，不能仅凭改文件后缀就保证可用。

## 4. 手动运行

使用虚拟环境的解释器，将下面的 `python` 替换成它的实际路径：

```text
python -I scripts/main.py --login all
python -I scripts/main.py --id 123 --tags "#4k、#高清"
python -I scripts/main.py --input "电报重登"
python -I scripts/main.py --input "夸克重登"
```

在任意其他工作目录执行也可以：给解释器和 `scripts/main.py` 使用绝对路径即可。配置、会话和下载目录始终位于该 skill 根目录，不会写到调用者的工作目录。

登录命令要保持运行。Telegram 打开 `.state/qr/telegram.png` 用手机 APP 扫码；夸克由官方 CLI 打开浏览器完成授权，浏览器没弹出或授权超时时，把授权页 URL 中 `code` 参数的值粘贴回终端。Telegram 两步验证在本地终端隐藏输入，或者只在本地进程环境设置 `TG_PASSWORD`。

## 5. 打包与迁移

打包脚本只用 Python 标准库，无需登录或访问网络：

```sh
python -I scripts/package_skill.py
```

默认在 **skill 目录内部的 `dist/`** 生成 `javhd-skill.zip`。也可以指定路径：

```sh
python -I scripts/package_skill.py --output /path/to/javhd-skill.zip
```

Windows PowerShell 示例：

```powershell
python -I scripts/package_skill.py --output D:/packages/javhd-skill.zip
```

默认不覆盖已有文件。确认要覆盖自己之前生成的包时，加 `--force`。脚本输出包路径、文件数、大小和 SHA-256，方便迁移后核验。

只打包明确列入清单的文档、脚本、测试、依赖文件和 **`config.example.json` 空白模板**。打包不读取、创建或修改本地 `config.json`，即使本地配置不存在也能打包。**不会打包**：

- 个人 `config.json`、自定义 Cookie 文件、额外配置备份或环境文件；
- `.state`、二维码、Telegram session、旧夸克 Cookie、官方 CLI 的授权配置；
- `.venv`、`node_modules`、下载视频、封面、预览、缓存或之前的 ZIP。

打包会携带固定的官方 CLI 文件，但不会携带它的授权信息。

脚本会检查模板是有效的 JSON 对象、包含完整且受支持的配置项，且凭据、频道、代理和 Cookie 路径保持空白默认值；误填个人信息时拒绝打包，错误消息不会回显填写值。并发数和通知开关也会校验类型与取值。请始终通过打包脚本生成 ZIP，不要直接压缩整个开发目录。

换机器或执行环境后：安装 Node.js 16+ → 重新创建虚拟环境 → 复制空白模板或单独私下迁移自己的 `config.json` → 核对频道、代理及 Cookie 路径 → 自检 → 重新完成 Telegram 扫码和夸克官方授权。向自己的已有安装迁移配置前先核对，不能覆盖目标机器原有配置。分发包不迁移任何登录态，Cookie 文件需要另行私下配置。

此前版本生成的 ZIP 可能仍含个人配置；修改源码不会清理已有旧包，需要用新打包脚本重新生成。

## 6. 真实 ID 解析测试（不上传）

只提取中文标题、封面、GIF/动态预览和完整 MP4 URL，默认终端仅输出这四项。不调用 Telegram/夸克，也不下载完整视频。默认 ID 为 `59152`：

```powershell
# 从 skill 根目录运行；Windows PowerShell
& .\.venv\Scripts\python.exe -I scripts/probe_video.py

# 明确指定 ID，另存封面和站点已提供的短预览
& .\.venv\Scripts\python.exe -I scripts/probe_video.py --id 59152 --save-assets

# 可选：使用自己有观看权限的站点 Cookie
& .\.venv\Scripts\python.exe -I scripts/probe_video.py --id 59152 --cookies-file D:/private/javhd-cookies.txt
```

Linux/macOS 将解释器换成 `.venv/bin/python`，去掉 PowerShell 的 `&`。代理和 Cookie 默认复用本地配置，不需要 Telegram 账号登录。

每次在 `.state/probes/{id}-随机串/` 新建独立详细报告。默认终端只显示四项；加 `--json` 输出详细 JSON。需要指定位置时使用 `--output 新目录`；已有目录会被拒绝，避免覆盖报告。报告包含实际中文标题和可能有时效签名的播放 URL，应留在本地，不提交或分发。

- `checks` 检查 `title_zh`、`cover_url`、`preview_url`、`full_video_url`，某项失败不妨碍其他字段返回。
- `success=true` 表示四项均已取得，包括站点提供的 MP4 动态预览；否则退出码为 1，表示部分或全部缺失，不一定是脚本异常。
- 从详情页解析内部 `videoId`，保留播放路由和其他参数，以 `is_trailer=0` 查询完整源并选取最高分辨率 MP4。仅使用当次 `sources[].src`，不生成签名；拒绝访问、无源或只有预告片时如实失败。
- `preview_url` / `preview_format` 与 `gif_url` 分开：短 MP4 不是原生 GIF，但属于有效动态预览。`gif_url` 只存原生 GIF，MP4 预览时该字段为空不表示缺失。`--save-assets` 另生成的 `generated_gif` 不冒充原生 URL。
- 页面和接口未给预览时，检查当前公开 ID 的 CDN 五秒预览；最多读取 64 字节，确认 MP4 文件头后才返回。不存在或返回 HTML 时仍记为缺失，不用推荐视频，也不下载完整视频来生成 URL。
- 默认仅提取完整源 URL，不证明完整文件可下载或全部内容完整。`--verify-video` 可限量读取 MP4 头尾检查响应和时长；页面时长仅作参考，不据此清空链接。`--save-assets` 只额外保存封面/已有短预览。

## 7. 夸克控制台上传测试

只测试夸克，不读取 Telegram 配置、不下载视频；测试类为 `QuarkUploadTest`，入口是 `tests/quark_upload_test.py`：

```powershell
# 在项目根目录执行；Windows PowerShell
& .\.venv\Scripts\python.exe -I tests/quark_upload_test.py

# 需要换账号或重新授权时
& .\.venv\Scripts\python.exe -I tests/quark_upload_test.py --relogin
```

Linux/macOS 使用 `.venv/bin/python -I tests/quark_upload_test.py`。

运行流程：

1. 校验内置官方 CLI 的版本和 SHA-256，并确认 Node.js 16+；不匹配时直接拒绝，不用未知运行时上传。
2. 用只读账号查询验证已保存授权。有效则直接复用；没有授权时才启动官方 `login`：浏览器打开的授权页完成确认即可。若浏览器未弹出或 60 秒内未完成，把授权页 URL 中 `code` 参数的值粘贴回终端，由官方 CLI 完成授权码登录。授权码本轮只使用一次，不会写进配置或分发给其他程序。
3. 官方 CLI 会把授权保存在其运行目录的上两级（本 skill 为 `.state/quark-cli`），不会进入 `vendor/`，也不打包。授权信息不要复制、公开或提交。
4. 输入本地文件完整路径，可带引号，例如 `"D:\Videos\测试.mp4"`。只接受可读的非空文件。
5. 输入网盘目录路径，例如 `/上传测试/资料`。缺失层级逐级创建；回车默认 `/上传测试`，输入 `/` 上传到根目录。
6. 显示官方 CLI 汇总的发送字节百分比、已发送/总大小、每秒刷新两次的速度和当前阶段。秒传单独显示，不伪造上传流量。只有官方最终结果返回文件 ID 才显示成功。

单次调用超时 30 分钟；超时会终止子进程并报告“上传未确认”，不会自动重传。官方错误码和消息会保留，但会过滤令牌、授权串等敏感字段。

本地原文件始终保留。`Ctrl+C` 可中止；若上传中断或失败，先检查网盘目标目录再重试，避免重复文件。此入口与主流程共用任务锁，已有任务运行时不会同时启动。下方离线测试不会执行此手动上传入口，也不会连接夸克服务。

## 8. 行为边界与验证

- Telegram 使用 512 KiB 分块、默认 6 个并发（上限 10 个在途请求）的有界流水线：确认一块立即补位、不停批等待，读盘在工作线程完成，内存只保留有限分块；cryptg 原生加密为必需依赖，避免退回纯 Python AES。普通账号/Premium 的单文件上限仍然适用。
- Telegram 不支持原始 GIF 动画进入普通视频相册，预览实际转为无声 MP4；站点没有提供当前视频预览时，从同一视频生成。
- 只处理网站本来提供的中文标题和已授权完整 MP4，不把预告片、HLS 或 HTML 冒充完整视频。
- 夸克上传固定使用内置的官方 CLI 1.0.20（`vendor/quarkclouddrive`），启动时校验 SHA-256；不以 Agent 身份绕过宿主识别，固定以 Claude Code 标识（`CLAUDECODE=1`）调用。升级必须先重新审查并跑通真实上传。
- 官方 CLI 自带运行诊断上报；本项目的适配器只转交命令参数和必要路径，不主动附加聊天内容。授权范围以夸克授权页为准。
- 两侧任务并行，一侧失败另一侧仍继续；结束后无论成功或失败都清理本次媒体。`cleaned=false` 会明确通知，强杀或断电不能保证清理。
- 重复运行同 ID 会重新发布。发布阶段结果不确定时先查目标，不自动重试整项任务。
- 最终通知只包含 ID 和真实状态，默认同时发到终端/agent 对话及 Telegram“收藏夹”。

离线验证：

```text
python -I -m unittest discover -s tests -v
```

把 `python` 替换为虚拟环境解释器。测试不登录、不发布，媒体测试只生成本地纯色视频。真实夸克授权、目标频道权限和线上上传仍需在配置完成后验收。
