# 抖音 / 小红书 Local API（轻量 Windows 版）

这是基于本仓库 Douyin 签名实现的轻量内网服务，**不依赖 Docker、PostgreSQL 或 Redis**。它提供以下能力：

- `GET /api/v1/douyin/users/{sec_user_id}/posts`：获取博主主页作品，支持 `cursor` 和 `count` 分页；
- `GET /api/v1/douyin/videos/{aweme_id}`：按作品 ID 获取作品详情；
- `POST /api/v1/cookies`：实时替换 Cookie，下一次请求立即使用新 Cookie。
- `POST /api/v1/accounts/login`：打开本机隔离浏览器窗口，扫码或手动登录一个抖音账号并自动保存会话；
- `GET /dashboard`：本地管理面板，集中显示服务、账号、检查结果与重登入口；`GET /api/v1/dashboard` 可供内网监控读取脱敏状态。
- `GET /api/v1/logs/requests`：查看最近的本地 API 请求元数据；Web 管理面板也提供最近请求视图。
- `GET /api/v1/accounts`：查看登录账号、失效和检查状态；`POST /api/v1/accounts/{account_id}/relogin` 可重新登录；`DELETE /api/v1/accounts/{account_id}` 可永久删除账号及其本地登录数据。
- `GET /api/v1/xhs/users/{user_id}/posts`：获取小红书博主主页作品列表；
- `GET /api/v1/xhs/notes/{note_id}`：按小红书笔记 ID 获取种草详情。

## 开发运行（macOS）

本机需使用 Python 3.11+。在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local.txt
python tools/fetch_xhs_assets.py
python -m local_api.main
```

默认监听 `0.0.0.0:8787`，内网其他机器可访问 `http://<本机内网IP>:8787/docs`。首次提交 Cookie 后会在 `data/accounts.json` 保存本地运行数据；不要共享、提交或同步这个目录。小红书签名模块需要 Node.js 20+；Windows 构建脚本会将 Node 与依赖一同放入 exe，因此最终使用 exe 无需另装 Node。

## 多账号网页登录

Windows 机器需要安装 Google Chrome 或 Microsoft Edge。Windows exe 启动后会显示原生桌面管理窗口：它显示服务状态、监听地址、**可直接复制给内网设备访问的 IPv4 地址**、账号总览、抖音/小红书数量、Cookie 最近检查时间、错误提示和重登入口；每 15 秒刷新一次，但不会显示 Cookie 或小红书临时令牌。原生窗口可一键复制内网 Web 面板地址；Web 面板的“服务信息”区域也会显示相同的内网地址。窗口中的“打开 Web 面板”按钮可访问 `http://127.0.0.1:8787/dashboard`（旧地址 `/accounts` 仍可使用）。

在面板选择抖音或小红书，输入账号备注并点击登录，程序会打开独立的浏览器配置目录。请在该窗口完成扫码或密码登录；小红书会额外验证“当前用户”接口，访客 Cookie 不会被误判为登录成功。验证成功后窗口自动关闭，账号会在列表中显示为 `active`。Cookie 不会显示在页面或接口响应中。优先使用 Chrome，未安装时自动尝试 Windows Edge；浏览器位于非标准目录时，可设置 `DOUYIN_LOCAL_API_BROWSER_PATH=C:\\Path\\To\\browser.exe`。

服务每 15 分钟检查已激活账号；你也可以在列表点击“检查”。确定失效的账号会显示 `needs_login`，点击“重新登录”即可打开该账号的新登录窗口。校验成功后，列表与 `GET /api/v1/accounts` 会显示所属账号的头像和昵称。两个平台的 Cookie 与浏览器配置彼此隔离；每个作品接口都可提交 `account_id` 指定使用某个登录账号，未指定时使用该平台最近更新的有效账号。删除账号需在面板确认，随后会永久清除该账号的 Cookie、隔离浏览器配置和小红书临时令牌缓存。

## 请求日志

服务会将业务 API 的请求元数据写入 `data/logs/requests.jsonl`，并在 Web 管理面板的“最近请求”区域展示。每条记录包含 UTC 时间、请求 ID、客户端 IP、HTTP 方法、**包含实际用户 / 作品 ID 的请求路径与地址**、状态码及耗时。例如显示 `http://192.168.1.10:8787/api/v1/xhs/notes/实际笔记ID`，而不是 `/api/v1/xhs/notes/{note_id}`。Cookie、请求头、请求体、查询参数和 `xsec_token` 不会写入日志；仪表盘轮询、健康检查和日志读取接口也不会污染业务日志。

内存、日志文件、日志 API 与面板均只保留 / 返回最近 **100 条**，按最新优先显示；超出自动淘汰。旧版轮转日志会在启动时合并裁剪为最近 100 条并清理旧轮转文件。旧记录没有保存的实际 ID 无法补回，新请求会记录真实地址。可从受信任内网调用（`limit` 默认为 100，允许 1–100）：

```bash
curl "http://127.0.0.1:8787/api/v1/logs/requests?limit=100"
```

## Windows 生成 exe

PyInstaller 不能从 macOS 交叉生成可靠的 Windows 可执行文件。把已开发好的项目复制到 Windows 后，双击或在命令行运行 [`build_windows.bat`](build_windows.bat)。脚本会先检测 Python 3.11+、Node.js 20+ 与可用的 Chromium 浏览器（Chrome 或 Edge）；缺少的构建依赖会通过 Windows `winget` 自动静默安装，再创建虚拟环境、安装 Python 依赖并打包。首次在干净电脑运行时，Windows 可能显示安装或 UAC 提示；系统没有 `winget` 时，脚本会停止并提示先安装 Microsoft App Installer。

成品是 `dist\\DouyinLocalAPI.exe`，可直接运行并打开 Windows 原生管理窗口；首次提交 Cookie 后会在 exe 同级目录建立 `data\\accounts.json`。成品为无控制台窗口程序；如需排障，可从命令提示符启动或临时使用源码运行。最终运行 exe 无需 Python 或 Node.js，但需要 Chrome 或 Edge 以便打开隔离登录窗口。

需要固定端口或数据目录时，可在启动前设置：

```bat
set DOUYIN_LOCAL_API_PORT=8787
set DOUYIN_LOCAL_API_DATA=D:\\DouyinLocalAPI\\data
DouyinLocalAPI.exe
```

如使用源码方式运行服务且不希望自动打开浏览器：

```bat
set DOUYIN_LOCAL_API_OPEN_BROWSER=0
python -m local_api.main
```

## 使用示例

Cookie 只会写入本地文件，响应绝不包含 Cookie 明文。

```bash
curl -X POST http://127.0.0.1:8787/api/v1/cookies \
  -H "Content-Type: application/json" \
  -d '{"cookie":"sessionid=...; msToken=...; s_v_web_id=..."}'

curl "http://127.0.0.1:8787/api/v1/douyin/users/MS4wLjABAAAA.../posts?count=20"

curl http://127.0.0.1:8787/api/v1/douyin/videos/1234567890123456789
```

## 小红书接口

两个读取接口共用自动选择策略，无需指定模式：

- **Python HTTP 读取**：使用已登录 Cookie 与签名，直接读取主页作品或笔记详情。
- **Playwright 页面读取**：每个账号独立复用无界面的浏览器及页面；通过打开主页、滚动加载、点击笔记卡片，读取网页实际产生的 JSON 响应，也支持读取页面已有的数据。读取过程中不会弹出窗口。

默认优先 HTTP；签名运行时不可用、网络/响应格式异常、令牌缺失或被拒绝时，自动尝试浏览器一次。切换后按“账号 + 接口”优先使用浏览器 15 分钟，之后自动探测 HTTP；浏览器普通故障也可回退 HTTP。主页 `items/cursor/has_more` 和详情 `item` 的外层结构保持一致。

小红书网页请求除 Cookie 外还要求临时 `xsec_token`。调用方仍可只传 `user_id` / `note_id`：服务自动缓存主页返回的笔记令牌；详情没有令牌时直接走页面读取，优先点击保留页面中的目标卡片。陌生纯笔记 ID 如果没有可访问入口、已删除或受限，仍可能返回 `422`，此时可提供当前分享链接。程序不会根据 ID 伪造令牌。

分页时请原样传回接口返回的 `cursor`，无需理解其内容。浏览器游标以 `pw_` 开头，按账号和博主隔离，有效期 30 分钟；重启、重新登录、更新 Cookie 或手动解析链接释放浏览器后，需从第一页重新读取。浏览器分页保留未返回的作品，避免网页每页数量与 `count` 不同造成漏数据。两条链路共用限流、冷却与结果缓存。首次打开浏览器和翻页可能较慢，调用方建议设置至少 120 秒读取超时。

Web 与 Windows 原生面板显示自动读取状态；`GET /api/v1/dashboard` 的 `service.xhs_read_strategy` 可查看各账号/接口最近使用和首选链路。网页登录、删除账号或替换 Cookie 前会关闭对应读取浏览器，避免配置目录被同时占用；服务关闭时自动释放所有读取浏览器。

### 461 / 471 请求频繁或验证提示

`461`、`471`（以及部分 `429`）不是普通网络超时，而是小红书的访问保护/验证信号。服务不会盲目重试：全部小红书 API 请求会在服务内串行执行，默认相邻请求至少间隔 8 秒；首次收到该类响应后暂停 5 分钟，连续触发时冷却逐级延长（15 分钟、45 分钟，最高 60 分钟）。冷却期间接口返回本地 `429` 和 `Retry-After` 响应头，避免多个内网调用方继续同时压测同一账号或网络。冷却状态保存为 `data/xhs-risk-state.json`，重启程序不会重置等待时间。

HTTP 风控失败后会记住浏览器优先策略，冷却结束后的下一次请求自动使用浏览器；不会在同一次风控失败后立刻换链路继续访问。如果浏览器也出现安全验证，账号显示 `needs_verification`（需要验证），通过现有“重新登录”窗口完成登录和平台验证后再继续，链路选择始终自动。

`300031` 则单独处理：它表示笔记当前无法打开，可能与链接令牌缺失、过期或内容受限有关。服务会丢弃被拒绝的本地令牌并尝试页面读取；若仍无法打开，返回 `422`。相同账号、笔记或主页分页的并发/重复读取会合并为一次读取，并分别在内存缓存 10 分钟/2 分钟。缓存按账号 Cookie 版本隔离。

可按实际业务量在启动前调整节奏，但不建议降低默认值：

```bat
set XHS_MIN_REQUEST_INTERVAL_SECONDS=8
set XHS_RISK_COOLDOWN_SECONDS=300
set XHS_POSTS_CACHE_SECONDS=120
set XHS_NOTE_CACHE_SECONDS=600
DouyinLocalAPI.exe
```

如仍持续出现 461，请在网页端完成平台要求的验证、等待冷却结束后再调用；不要并发轮询或反复重试，也不要将其误判为 Cookie 失效。

也可调用 `POST /api/v1/xhs/resolve-link`，传入笔记、博主主页或 `xhslink.com` 分享链接。此接口会打开隔离浏览器跟随跳转并返回 `xsec_token`、`xsec_source`、`user_id` 和 `note_id`；适用于后台静默解析未能取得令牌时的手动兜底。账号必须先通过 `/accounts` 完成网页登录，以便存在可复用的隔离浏览器配置。

```bash
# 博主主页作品列表
curl "http://127.0.0.1:8787/api/v1/xhs/users/USER_ID/posts?count=20"

# 笔记/种草详情
curl "http://127.0.0.1:8787/api/v1/xhs/notes/NOTE_ID"
```

小红书读取能力基于 [Nirvana919/XHS_Crawler](https://github.com/Nirvana919/XHS_Crawler) 的 MIT 许可签名运行时进行适配；详细归属见 `local_api/xhs_assets/THIRD_PARTY_NOTICES.md`。

`sec_user_id` 是主页 URL 中 `/user/` 后面的稳定 ID；抖音作品列表上游接口并不接受会变动的纯数字 `uid`，因此不能用数值 UID 替代它。`/health` 只显示服务状态和 Cookie 是否已配置。

## 安全与运维

- 所有业务接口均无需鉴权，因此只将端口放行给可信内网。任何能访问服务的人都能发起浏览器登录、使用或替换会话。
- Cookie 具有账号权限。不要把 `data/` 目录共享、提交到 Git 或放进网盘。
- 请求日志含内网客户端 IP 和调用时间，也应作为运行数据妥善保管；它不含 Cookie 或平台令牌。
- 服务热更新 Cookie 是原子写入的；并发请求只会使用旧 Cookie 或完整的新 Cookie，不会读到半截内容。
- 平台的风控、登录态过期和接口变更仍可能导致 `502`。遇到此情况先使用已登录浏览器重新导出 Cookie，再调用更新接口。
