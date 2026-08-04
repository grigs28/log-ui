# log-ui

> 基于 **VictoriaLogs** 的自建日志展示 / 运维 UI：**yz-login 单点登录**、总览大屏图表、日志搜索（自动刷新）、服务器纳管、实时 tail。
> 后端复用现有 VictoriaLogs + Vector + Cobian，UI 完全自主、方便调整。

---

## 功能特性

| 模块 | 能力 |
|---|---|
| **SSO 登录** | yz-login ticket 免密登录 / 登出 / 会话；未登录自动跳转 |
| **总览大屏** `/` | 统计 tile（总量/错误/警告/服务器数）+ ECharts：日志量趋势、等级分布、各服务器日志量、各服务器错误数；可切 1/3/7/30 天 |
| **日志搜索** `/search` | 服务器/等级/LogsQL 关键词过滤，结果按等级着色；**自动刷新**（关/5/10/30/60s，局部轮询不整页闪） |
| **服务器管理** `/servers` | 注册服务器 + **自动发现**主机；每台 IP/类型/活跃状态/7天日志量/错误数/最近日志；管理员增删（写 `servers.yaml`） |
| **实时 tail** `/tail` | WebSocket 实时日志流，最新在上，按等级着色，自动滚动 |
| **设置** `/settings` | 个人偏好（默认刷新/条数/主题深浅）+ SSO 配置（管理员可改 yz-login 地址/应用ID，免重启生效） |
| **运行** | systemd 托管（开机自启+崩溃自愈），默认端口 80 |

---

## 架构

```
浏览器 ──(SSO)──> yz-login ──ticket──> log-ui (FastAPI, :80)
                                         │
                                         ├──> VictoriaLogs API (:9428)
                                         │      /select/logsql/{query, stats_query, stats_query_range, field_values}
                                         ├──> 服务器清单 (config/servers.yaml + field_values 自动发现)
                                         └──> 个人偏好 (config/prefs/<user>.json)
```

**不动数据层**（VictoriaLogs / Vector / Cobian）。log-ui 只是 VL 之上的一层「读 + 展示 + 纳管」。

---

## 依赖

- **VictoriaLogs**（已部署，提供查询 API）
- **Python 3.11+**（本项目用 conda 环境 `log-ai`）
- **yz-login**（统一登录平台；用于 SSO）
- 见 `requirements.txt`：FastAPI、uvicorn、Jinja2、httpx、itsdangerous、PyYAML、python-multipart
- **ECharts 5**（总览图表，已随仓库提供 `static/echarts.min.js`）

---

## 快速部署

```bash
# 1. 配置（从模板复制并填写）
cp config/settings.yaml.example config/settings.yaml
vi config/settings.yaml          # 填 vl_url / yz_login_url / yz_app_id / secret_key

# 2. 装依赖
/opt/conda3/envs/log-ai/bin/pip install -r requirements.txt

# 3. systemd 托管（绑 80 需 root；或改 listen_port 避开特权端口）
sudo cp log-ui.service /etc/systemd/system/   # 见下方"systemd 单元"
sudo systemctl daemon-reload && sudo systemctl enable --now log-ui

# 4. 放行端口
sudo firewall-cmd --permanent --add-port=80/tcp && sudo firewall-cmd --reload
```

访问 `http://<服务器IP>/`。

> 在 yz-login 管理后台注册本应用，回调 URL 填 `http://<服务器IP>/auth/callback`（或根 `/`），把应用 ID 填进 `settings.yaml` 的 `yz_app_id`。

### systemd 单元（`log-ui.service`）
```ini
[Unit]
Description=log-ui (log viewer on VictoriaLogs, yz-login SSO)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/logging/log-ui
ExecStart=/opt/conda3/envs/log-ai/bin/uvicorn app.main:app --host 0.0.0.0 --port 80
Restart=always
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
（非 root 绑 80：用 `AmbientCapabilities=CAP_NET_BIND_SERVICE` 或 `setcap`。）

---

## 配置说明

### `config/settings.yaml`（运行配置，含密钥，**勿入库**）
| 键 | 说明 |
|---|---|
| `vl_url` | VictoriaLogs API 地址（同机用 `http://localhost:9428`） |
| `yz_login_url` | yz-login 平台地址 |
| `yz_app_id` | 在 yz-login 注册的本应用 ID |
| `callback_url` | 登录回调地址 |
| `listen_host` / `listen_port` | 监听（默认 `0.0.0.0:80`） |
| `secret_key` | session 签名密钥（`openssl rand -hex 32` 生成） |

> SSO 的 `yz_login_url` / `yz_app_id` 可在 **设置页**（管理员）在线修改，写回 `settings.yaml`，免重启。

### `config/servers.yaml`（服务器纳管清单）
```yaml
servers:
  - name: DS1821plus        # 必须与日志里的 hostname 字段一致
    ip: 192.168.0.79
    type: nas
    note: 群晖 NAS
```
未登记但已发日志的主机会在「服务器」页自动出现（标"未注册"）。

### `config/prefs/<user>.json`（个人偏好，运行时生成）
默认自动刷新间隔、默认条数、主题。

---

## 项目结构

```
log-ui/
├── README.md
├── requirements.txt
├── log-ui.service            # systemd 单元（示例）
├── config/
│   ├── settings.yaml.example # 配置模板（入库）
│   ├── settings.yaml         # 实际配置（gitignore）
│   ├── servers.yaml          # 服务器纳管清单
│   └── prefs/                # 个人偏好（gitignore）
├── static/
│   └── echarts.min.js        # 图表库
├── app/
│   ├── main.py               # FastAPI 入口 + 路由（总览/搜索/服务器/tail/设置）
│   ├── auth.py               # yz-login SSO（ticket 回调/会话）
│   ├── vl.py                 # VictoriaLogs API 客户端（查询 + 聚合）
│   ├── servers.py            # 服务器纳管注册表
│   ├── prefs.py              # 个人偏好读写
│   └── config.py             # 配置加载 + SSO 配置在线读写
└── templates/                # Jinja2：base/overview/index/_logs/servers/tail/settings
```

---

## 技术栈

- **后端**：Python FastAPI + uvicorn
- **前端**：Jinja2 + 原生 JS + ECharts（无构建步骤，单进程，最易调整）
- **实时**：FastAPI WebSocket
- **认证**：yz-login（自定义 ticket 协议）

---

## 路由

| 路径 | 说明 |
|---|---|
| `/` | 总览大屏 |
| `/search` | 日志搜索 |
| `/logs` | 日志表格片段（自动刷新轮询） |
| `/servers` | 服务器纳管 |
| `/tail` · `/ws/tail` | 实时 tail 页 · WebSocket |
| `/settings` · `/settings/sso` | 个人偏好 · SSO 配置（管理员） |
| `/auth/login` · `/auth/callback` · `/auth/logout` | yz-login SSO |

---

## 与现有系统的关系

- **复用**：VictoriaLogs（存储）、Vector（采集）、Cobian reader（备份日志）、docker 栈。
- **并存**：Grafana / vmui 保留作兜底与深度查询。
- **新增**：log-ui = 面向运维的统一入口（SSO + 总览 + 纳管 + 实时）。
