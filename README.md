# wechat-assistant

Windows 本机运行的**微信群消息自动发送服务**（只发不收）。业务侧在云端生成待发消息，本机定时拉取并通过微信客户端投递到指定群，然后回执云端。

## 架构

```
业务触发（小程序 / 云端定时扫描）
        ↓
云函数 push-wechat  →  云数据库 wechat_outbox
        ↓  （本机主动轮询，无需公网入口）
本机 pull-wechat 拉取
        ↓
本地 SQLite 队列（queue.db）
        ↓
微信客户端发送（wcferry hook 或 wechatauto UIA）
        ↓
ack 回执云端
```

三个后台线程：

| 线程 | 职责 |
|---|---|
| `poll_loop` | 按 `poll_interval_sec` 轮询云端，把待发消息入本地队列（msg_id 幂等去重） |
| `worker_loop` | 串行消费队列；失败退避重试 10s/60s/180s/300s，微信离线类错误快速失败 |
| `watcher_loop` | 每 60s 检查微信在线状态、自动还原/拉起微信窗口、重置卡死超过 120s 的 sending 记录 |

## 环境要求

- Windows 10/11
- Python 3.10+（建议 3.12）
- 已登录的微信桌面客户端
  - `engine: wcferry` → 需要微信 **3.9.12.x**（API 级注入，不依赖窗口可见，最稳）
  - `engine: rpa` → 支持微信 4.x（UIA 操作，**要求微信主窗口保持可见、不最小化**）

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置

```powershell
Copy-Item config.example.yaml config.yaml
```

然后编辑 `config.yaml`，至少填写：

| 字段 | 说明 |
|---|---|
| `cloud.pull_url` | 云函数 HTTP 触发器地址 |
| `cloud.token` | 与云端 `SENDER_TOKEN` 环境变量一致；**不要提交真实值** |
| `wechat.engine` | `wcferry` 或 `rpa` |
| `wechat.groups` | 群白名单，只允许向这些群发送 |
| `watch.wechat_exe` | 微信可执行文件路径（用于掉线自动拉起） |
| `alerts.wechat_work_webhook` | 可选，企业微信群机器人 webhook，留空则只写日志 |

`config.yaml` 已加入 `.gitignore`，不会被提交。

## 启动

```powershell
# 前台调试（可见输出）
.\.venv\Scripts\python.exe -u app.py

# 后台运行（无窗口）
start "" .venv\Scripts\pythonw.exe -u app.py
```

或直接双击 `run-sender.bat`（内部含单实例检查）。

开机自启：把 `run-sender.bat` 的快捷方式放入
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`。
不再需要时把该 `.lnk` 重命名为 `.lnk.disabled` 即可，不必删除。

## 运行时文件位置

| 文件 | 路径 | 说明 |
|---|---|---|
| 消息队列 | `./queue.db` | SQLite，表 `messages`（`msg_id / grp / text / status / error / created_at / updated_at`） |
| 服务日志 | `./service.log` | 主日志，含入队/发送/失败记录 |
| 健康快照 | `./health.log` | `health-watch.py` 每 5 分钟追加 |
| 微信自动化日志 | `./wechatauto_logs/` | wechatauto 引擎自动生成 |
| 单实例锁 | `./.app.pid.lock` | 进程退出后为僵尸锁，可直接删除 |

队列状态机：`pending → sending → done | failed`。
服务重启时会把残留的 `sending` 重置为 `pending` 续发（业务侧需容忍极少数重复发送）。

```powershell
# 查看队列概况
.\.venv\Scripts\python.exe -c "import store;print(store.queue_depth())"
```

## 健康监控

```powershell
.\.venv\Scripts\pythonw.exe -u health-watch.py
```

每 5 分钟往 `health.log` 写一条快照：进程 PID、微信窗口可见性、队列计数、最近 WARNING。

## 已知限制

- **RPA 引擎依赖窗口可见**：微信被最小化或在离屏坐标时 UIA 看不到控件，发送会失败。服务内已做自动还原（`ShowWindow` + `SW_RESTORE`），但锁屏仍会导致失败，需保持不锁屏。
- **微信 4.x 的 Qt6 会丢失 `WS_VISIBLE` 样式位**：窗口看着在屏幕上但 `IsWindowVisible=False`，`_restore_wechat_window()` 已修复该情况。
- `wcferry` 仅支持微信 3.9.12.x，4.x 无法注入。
- 群名必须与微信群列表中的名称完全一致（白名单 + 精确匹配双重校验，防止发错群）。

## 可选：内网穿透（旧方案）

`optional/frp/` 下保留了早期「云端反向推送」架构所需的 frps 部署脚本与客户端配置样例。当前默认的轮询架构**不需要**它。

## 安全说明

- 仓库内所有 token / webhook / 服务器地址均为占位符，请自行填写。
- 群白名单 + 精确会话校验，避免误发到非目标群。
- `msg_id` 幂等，云端超时重放不会造成本地重复入队。
