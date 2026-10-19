# 抖音 / 小红书 Local API（轻量 Windows 版）

这是基于本仓库 Douyin 签名实现的轻量内网服务，**不依赖 Docker、PostgreSQL 或 Redis**。它提供以下能力：

- `GET /api/v1/douyin/users/{sec_user_id}/posts`：获取博主主页作品，支持 `cursor` 和 `count` 分页；
- `GET /api/v1/douyin/videos/{aweme_id}`：按作品 ID 获取作品详情；
- `POST /api/v1/cookies`：实时替换 Cookie，下一次请求立即使用新 Cookie。
- `POST /api/v1/accounts/login`：打开本机隔离浏览器窗口，扫码或手动登录一个抖音账号并自动保存会话；
- `GET /dashboard`：本地管理面板，集中显示服务、账号、检查结果与重登入口；`GET /api/v1/dashboard` 可供内网监控读取脱敏状态。
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

小红书网页请求除 Cookie 外还要求临时 `xsec_token`。主页作品接口可直接只传 `user_id`；服务会从结果中缓存每篇笔记的临时令牌，因此对该列表返回的 `note_id` 调用详情接口时也只需传 ID。令牌不会出现在接口响应中。对于未曾经由本服务拉取过的笔记 ID，详情接口会在后台使用已登录的隔离配置**无界面静默尝试**获取令牌，不会弹出浏览器窗口。若小红书未对该纯 ID 签发令牌，才返回 `422`；此时请先拉取该博主主页列表、将页面链接中的 `xsec_token` 与 `xsec_source` 作为可选查询参数传入，或手动调用链接解析接口。

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
- 服务热更新 Cookie 是原子写入的；并发请求只会使用旧 Cookie 或完整的新 Cookie，不会读到半截内容。
- 平台的风控、登录态过期和接口变更仍可能导致 `502`。遇到此情况先使用已登录浏览器重新导出 Cookie，再调用更新接口。
