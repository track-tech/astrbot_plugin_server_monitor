# astrbot_plugin_server_monitor

服务器监控插件 —— 在 AstrBot WebUI 的插件 Pages 页面实时查看本机与云服务器的
CPU / 内存 / 磁盘 / 网络 / 负载状态，支持聊天指令查询与阈值告警推送。

## ✨ 功能

- **模块化面板看板（v1.3）**：WebUI 由面板自由拼接而成，内置 8 种面板——
  服务器卡片 / 历史图表 / 系统信息 / Top 进程 / Docker 容器 / 端口探活 / HTTP 探活 / 告警记录。
  每个面板可拖拽排序、右下角把手拖拽调整宽度（¼~整行）与高度、切换数据源、独立设置刷新间隔（3 秒 ~ 5 分钟）；
  终端面板带自动滚动开关；
  顶栏可自由设定全局刷新频率（1~3600 秒）。
  **预设布局**：顶栏「布局」菜单内置 5 套预设——默认布局 / 运维优先 / 模型观测 / 紧凑总览 / 大屏监控，一键切换；
  **自定义布局**：排布好后可在同一菜单里命名保存（保存在浏览器本地，支持多个方案），随时应用或删除。
- **运维探活（v1.3）**：在插件配置中添加 TCP 端口与 HTTP 服务探活列表，插件按周期探测连通性、
  状态码并测量延迟，结果在看板面板中实时展示。
- **Docker 容器 / Top 进程（v1.3）**：本机、SSH、Agent 三种采集端均可上报容器状态与 CPU 占用
  Top 进程（可在配置中关闭）。
- **模型调用统计（v1.4）**：自动挂钩 AstrBot 的 LLM 请求/响应钩子，统计今日调用数、
  成功/失败、输入/输出 Tokens、**缓存命中率**、平均延迟、近 5 分钟调用量与每分钟调用曲线、
  按模型分组统计、最近调用列表；按日聚合持久化到 AstrBot 数据目录（重启不丢，保留 90 天）。
  聊天指令 `/model` 查看，`/model history` 看近 7 天趋势。
- **模型余额（v1.4）**：支持 DeepSeek / Moonshot (Kimi) / SiliconFlow / OpenAI Billing / **One-API·New-API 中转站**，
  在插件配置中填入 API Key 即可定期查询余额，看板面板实时展示（含赠余额、已用、错误状态）。
- **Docker 容器**（v1.7.3 增强）：本机 / SSH / Agent 三端采集容器列表；采集失败时面板
  直接显示具体原因（未安装 CLI / 权限不足 / 守护进程无响应），不再笼统显示"没有容器"。
- **网页 SSH 终端（v1.7）**：看板「终端」面板升级为**持久交互式会话**——内嵌 xterm.js
  真终端（颜色/光标/回显完整），选择本机或任意 SSH 模式的服务器连接（SSH 走远端 PTY，
  体验与真 ssh 一致；本机为持久 shell，cd/环境变量跨命令保留）；输出经 SSE 实时推送。
  会话闲置 10 分钟自动回收，面板关闭/页面刷新即断开；建立/关闭写入审计日志。
  出于安全考虑可在配置中关闭（`terminal_enabled`），请务必保管好面板密码。
- **告警记录**：最近 50 条告警/恢复事件在看板面板中回看，同时保留聊天推送。
- **本机监控**：基于 psutil，采集 CPU、内存、Swap、磁盘（多分区）、网络上下行速率、磁盘 IO、
  负载、温度、运行时长，以及 AstrBot 进程自身占用。
- **云服务器监控**（二选一或混用）：
  - `SSH` 模式：定期通过 SSH 在远端执行**只读**采集脚本（无需在被监控机安装任何东西），仅支持 Linux；
  - `Agent` 模式：在被监控机上运行本插件 `agent/astrbot_srv_agent.py`（单文件、仅依赖 psutil），
    插件通过 HTTP 拉取，支持 Linux / Windows / macOS。
- **透明界面**：页面无自绘背景（适配 astrbot_plugin_palette 等壁纸/透明美化主题），半透明毛玻璃
  卡片 + 文字投影，全部图标为现代线性 SVG 矢量图标，自动跟随 AstrBot 面板明暗主题。
- **聊天查询**：`/server`（别名 `服务器状态`）、`/server detail`。
- **告警推送**：CPU / 内存 / 磁盘使用率超阈值、远程服务器离线时推送到指定会话，
  支持连续超标次数、告警冷却与恢复通知。

## 📦 安装

1. 将本目录放入 AstrBot 的 `data/plugins/` 下（或通过插件市场/从 URL 安装）；
2. 确认依赖安装成功（`requirements.txt`：`psutil`、`asyncssh`）；
3. 在 WebUI「插件管理」中启用插件。

## 🖥️ WebUI 页面

启用插件后，在 AstrBot 管理面板左侧「插件」分类下会出现 **服务器监控** 页面：

- 顶部可切换自动刷新间隔（2/3/5/10/30 秒）或手动刷新；
- 每台服务器一张卡片：CPU / 内存 / 磁盘圆环仪表、负载、网络速率、Swap、温度、
  磁盘分区明细、迷你趋势线；离线服务器会显示错误原因；
- 下方「历史趋势」可选择服务器查看 CPU/内存/Swap 使用率曲线与网络速率曲线。

## 💬 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/server` 或 `服务器状态` | 查看所有服务器状态概览 |
| `/server detail` | 详细信息（含全部分区、温度、Top 进程） |
| `/server bind` | **在目标聊天中发送**，绑定当前会话接收告警（并自动开启告警） |
| `/server unbind` | 解除告警绑定 |
| `/server test` | 向绑定会话发送一条测试消息 |
| `/model` | 查看今日模型调用统计（缓存命中率、延迟等） |
| `/model history` | 查看近 7 天调用趋势 |

## ☁️ 接入云服务器

### 方式一：SSH（零安装）

插件配置 → 远程服务器列表 → 添加「SSH 服务器」模板，填写名称、主机、端口、用户名，
认证二选一：填 `password`（密码），或填 `key_path`（本机私钥文件路径，填写后优先使用）。

> 注意：
> - 仅支持 Linux 远端（采集脚本读取 `/proc`）；
> - 出于易用性考虑默认**跳过 host key 校验**（`known_hosts=None`），
>   请使用专用低权限账号、密钥认证并限制来源 IP；
> - 远端账号需能读取 `/proc`（普通账号即可，无需 root）。

### 方式二：HTTP Agent（推荐用于 Windows 云主机或不想开 SSH 的场景）

1. 将 `agent/astrbot_srv_agent.py` 上传到云服务器，安装依赖并启动：
   ```bash
   pip3 install psutil
   python3 astrbot_srv_agent.py --port 9122 --token 换成你的令牌
   ```
   建议用 systemd 常驻（`/etc/systemd/system/srvmon-agent.service`）：
   ```ini
   [Unit]
   Description=AstrBot Server Monitor Agent
   After=network.target

   [Service]
   ExecStart=/usr/bin/python3 /opt/srvmon/astrbot_srv_agent.py --port 9122 --token 换成你的令牌
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   systemctl daemon-reload && systemctl enable --now srvmon-agent
   ```
2. 放行防火墙/安全组端口（建议仅对 AstrBot 所在 IP 放行）；
3. 插件配置 → 远程服务器列表 → 添加「HTTP Agent 服务器」模板，
   `url` 填 `http://<服务器IP>:9122/metrics`，`token` 与启动参数一致。

## 🔔 告警

在目标聊天（群聊/私聊）中发送 `/server bind`，然后按需在插件配置中调整：

- 阈值：CPU（默认 85%）、内存（默认 90%）、磁盘（默认 90%）；
- `sustain_times`：连续超标 N 次才告警（防抖，默认 3）；
- `cooldown`：同一告警冷却时间（默认 1800 秒）；
- `notify_recovery` / `notify_offline`：恢复通知与离线告警开关。

> 修改采样间隔、服务器列表等配置后，请在插件管理中「重载插件」使其生效。

## ⚙️ 配置参考

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `local_monitor` | true | 启用本机监控 |
| `local_interval` | 5 | 本机采样间隔（秒） |
| `remote_interval` | 60 | 远程服务器采样间隔（秒） |
| `history_points` | 360 | WebUI 历史曲线保留点数 |
| `remote_servers` | [] | 远程服务器列表（SSH / Agent 模板） |
| `alerts.*` | - | 告警开关、阈值、推送会话、冷却等 |
| `probe_interval` | 30 | TCP / HTTP 探活执行周期（秒） |
| `tcp_probes` | [] | TCP 端口探活列表（名称 / 主机 / 端口） |
| `http_probes` | [] | HTTP 服务探活列表（名称 / URL / 方法 / 期望状态码） |
| `terminal_enabled` | true | 启用 WebUI 终端（面板登录后可执行命令） |
| `terminal_timeout` | 30 | 终端单条命令超时（秒） |
| `terminal` 面板 | - | 看板添加「终端」面板即可使用；SSH 目标直接复用监控配置的凭据 |
| `collect_docker` | true | 采集 Docker 容器列表 |
| `collect_procs` | true | 采集 Top 进程列表 |
| `llm_balance_interval` | 600 | 模型余额查询周期（秒） |
| `llm_balance_sources` | [] | 模型余额来源（DeepSeek / Kimi / SiliconFlow / OpenAI Billing / One-API 中转）。API 地址带不带 `/v1` 均可，自动尝试并记忆；中转站类型需填系统访问令牌（非 sk- Key），New-API 需另填用户 ID |

## ❓ 常见问题

- **升级后报 `unexpected keyword argument` 等版本错位错误**：插件热重载只会重新执行 main.py，旧的子模块可能仍缓存在内存中（v1.4.2 起已自动清除缓存）。若仍遇到，**完全重启 AstrBot** 即可解决。
- **页面提示「监控服务尚未就绪（启动失败: …）」**：括号内就是真实异常原因；AstrBot 日志中同时会有 `[server_monitor] 启动监控服务失败` 的完整堆栈。常见原因：配置文件损坏（删除 `data/config/astrbot_plugin_server_monitor_config.json` 后重载插件可重置）、依赖未装全（psutil / asyncssh）。把括号内的信息反馈即可精确定位。
- **看板数据不更新（v1.1 已多重加固）**：
  - 页面右上角显示「已连接 · 更新于 hh:mm:ss」代表轮询正常；若卡片底部出现黄色「数据滞后」提示，
    说明后端快照变旧——v1.1 起后端在每次页面拉取时会**按需补采**本机快照（自愈），远程数据按
    `remote_interval` 周期更新属正常；
  - 页面请求附带时间戳参数以穿透可能的 GET 缓存；轮询循环失败也会自动重试，不会停摆；
  - 仍异常时请在插件管理中「重载插件」，并查看日志中是否有 `Web API 已注册` /
    `监控服务已启动` / 采样失败等信息。
- **页面显示「连接失败」**：确认插件已启用且日志中无 `注册 Web API 失败`；
  插件 Pages 与 Web API 需要 AstrBot v4.10+。
- **SSH 采集失败**：先手动 `ssh user@host` 确认可连通；查看卡片上的错误信息
  （超时 / 认证失败 / 无 /proc）。Windows 远端请改用 Agent 模式。
- **Agent 显示离线**：检查端口放行、token 是否一致，浏览器访问
  `http://IP:9122/metrics` 应返回 JSON。

## 📄 许可

MIT
