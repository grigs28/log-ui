# Docker 部署方案（日志采集存储栈）

> VictoriaLogs + Vector + Grafana 三容器日志栈，docker-compose 管理。
> 本目录是把 `/opt/logging` 的实际部署整理成的可复现方案。

## 架构

```
journald / syslog(UDP514) / TAF(UDP515) / rclone(TCP516) ──> Vector(采集+转换) ──> VictoriaLogs(:9428) ──> Grafana(:3000)
Cobian 备份日志(宿主脚本, 每5分钟) ────────────────────────────────────────────────> VictoriaLogs
```

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 三服务：victorialogs / vector / grafana（`logging` 桥接网络） |
| `vector.toml` | Vector 管道：4 个 source + 各自 remap transform → VL sink |
| `provisioning/datasources/victorialogs.yml` | Grafana 自动 provision VictoriaLogs 数据源 |
| `scripts/cobian_log_reader.py` | Cobian 备份日志采集器（宿主 systemd 运行，非容器） |

## ⚠️ 关键约束（按本机踩坑总结）

1. **用 OpenEuler 原生 `docker-engine 18.09`，不要装 docker-ce**
   本机 CPU 缺 `x86-64-v2` 指令集，docker-ce 的 containerd 报 `CPU ISA level is lower than required` 跑不起来。
   ```bash
   dnf -y install docker-engine   # 注意：docker-ce 仓库要禁用，否则 docker-engine 名字会被劫持成 docker-ce
   ```
2. **firewalld：把 docker 网段加入 trusted 区**（否则同网络容器互访被拦，Grafana 查 VL 报 no route to host）
   ```bash
   firewall-cmd --permanent --zone=trusted --add-source=172.16.0.0/12
   firewall-cmd --reload
   ```
   （`net.bridge.bridge-nf-call-iptables=1` 时容器桥接流量会过 firewalld。`systemctl restart docker` 不能修这个，必须加 trusted source。）

## 部署步骤

```bash
# 0. 装docker-engine（见上），启动并 enable
systemctl enable --now docker

# 1. 部署目录 + 数据目录
mkdir -p /opt/logging/{victorialogs-data,grafana-data,provisioning/datasources}
cp docker-compose.yml vector.toml /opt/logging/
cp -r provisioning /opt/logging/
cp scripts/cobian_log_reader.py /opt/logging/scripts/   # Cobian 采集器（见下）
chown -R 472:472 /opt/logging/grafana-data              # Grafana 容器 UID 472

# 2. 改 docker-compose.yml：把 GF_SECURITY_ADMIN_PASSWORD 的 CHANGE_ME 改成你的密码

# 3. 起栈
cd /opt/logging && docker-compose up -d

# 4. firewalld（trusted + 放行端口）
firewall-cmd --permanent --zone=trusted --add-source=172.16.0.0/12
firewall-cmd --permanent --add-port=3000/tcp --add-port=9428/tcp   # Grafana / VL API
firewall-cmd --permanent --add-port=514/udp --add-port=515/udp --add-port=516/tcp  # 日志接入
firewall-cmd --reload
```

## 端口

| 端口 | 用途 |
|---|---|
| 9428 | VictoriaLogs（写入 + 查询 API） |
| 9242 | VictoriaLogs（辅助） |
| 3000 | Grafana |
| 514/udp | syslog（Linux/群晖/网络设备） |
| 515/udp | TAF（JSON） |
| 516/tcp | rclone syslog |

## 数据持久化（bind-mount，勿删）

- `/opt/logging/victorialogs-data` → VictoriaLogs 数据（保留期 `-retentionPeriod=180d`）
- `/opt/logging/grafana-data` → Grafana DB / 插件 / 配置（UID 472）

## Cobian 备份日志采集器（宿主，非容器）

Cobian 日志在 Windows(192.168.0.28) 的 SMB 共享，由**宿主**脚本读取（容器内不好挂 SMB）：

```bash
# 1. 挂载 SMB（/usr/local/bin/mount-cobian.sh，凭据自行填写）
mount -t cifs '//192.168.0.28/c$/.../Cobian Backup 11/Logs' /mnt/cobian-logs -o username=...,vers=3.0

# 2. systemd timer 每5分钟跑采集器
# cobian-log-collector.timer -> cobian-log-collector.service
#    ExecStart=/opt/conda3/envs/log-ai/bin/python /opt/logging/scripts/cobian_log_reader.py
systemctl enable --now cobian-log-collector.timer
```
采集器读 `/mnt/cobian-logs`（UTF-16），POST 到 VictoriaLogs，状态存 `/var/lib/cobian-log-state.json`。
**健壮性**：POST 失败不前进游标，下次重读（VL 宕机不丢）。

## 验证

```bash
docker-compose ps                                # 三容器 Up
curl -o /dev/null -w '%{http_code}\n' localhost:9428/health     # VictoriaLogs 200
curl -o /dev/null -w '%{http_code}\n' localhost:3000/api/health # Grafana 200
curl -s localhost:9428/select/logsql/query -d 'query=*' -d 'limit=3'   # 查到日志
```

## 数据/字段约定（写新采集源必读）

- VictoriaLogs 写入：POST `http://victorialogs:9428/insert/jsonline`，newline-delimited JSON
- `_msg`（消息）/ `_time`（毫秒时间戳）必需；**`_stream` 是保留字段，含则记录被丢弃**
- 其他顶层字段（`hostname`/`app_name`/`level`/`log_source`）成为可索引 stream 字段
- **时区**：外部源多为 CST，vector 把无时区本地时间按 UTC 解析后**减 8 小时**（vector.toml 里的 `-28800`/`-8h`，勿删）

## 加新日志源（增减服务器）

在 `vector.toml` 加 `[sources.xxx]` + `[transforms.xxx_prep]`，并把 transform 名加进 `[sinks.victorialogs]` 的 `inputs`。设备把 syslog 指向本机 514/516 即自动纳入；新主机在 Grafana 变量 / log-ui 的「服务器」页自动出现。

## 已知坑

- docker-ce 跑不了（CPU ISA，见上）→ 用 docker-engine 18.09
- firewalld 拦容器互访 → docker 网段加 trusted
- Grafana 数据目录权限 → 472:472
- Cobian 采集器在系统停机期间若 VL 宕机会"读过未入库"→ 已修复（POST 失败不前进游标）
