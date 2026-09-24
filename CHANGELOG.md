







# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [0.5.8] - 2026-09-24

### Changed
- 总览页采集器徽章样式升级：pill 徽章（状态色文字 + 同色发光底/描边），
  字号 11px→13px 加粗，比原先的灰色小字更醒目；悬停仍显示详情

## [0.5.7] - 2026-09-24

### Fixed
- vecsync 策略条件生成：if-else 链压成单行——`}` 换行后独立成行的 `else`
  在 Vector 0.54 实测触发 E203（0.5.6 部署时 sync 失败已自动回滚，功能未生效，
  本版为修正后的正式落地）

## [0.5.6] - 2026-09-24

### Added
- **按主机配置采集级别**：新建/编辑服务器可设最低级别（info+ 全收 / warning+ / error+，
  缺省 info+ 全收）。纳管表与忽略列表新增「级别」列；保存后自动生效（重启 Vector 数秒）
  - Vector 受管块由"忽略名单"升级为"主机策略表"（忽略 > 级别 > 默认放行），
    生成多行 VRL（VRL 不支持分号单行，此前写法无法编译）
  - Cobian 采集器同步按 `192.168.0.28` 的级别设置过滤（绕过 Vector 的路径单独设闸）
  - 忽略时级别设置随 IP/类型一并带入忽略列表，删除后原样恢复

### Fixed
- 采集器徽章误报 vector down：docker 29.x 的 `--format` 无 `.State.RestartCount`
  字段（模板报错导致判定失败）；改用 `{{.State.Status}}` 判定

## [0.5.5] - 2026-09-24

### Added
- **总览页采集器状态徽章**（小徽章，悬停看详情）：vector / cobian / victorialogs
  - vector 双层判定：容器 Running + 最近 10 分钟入库量（进程 Up 但零流量 = 僵尸态，
    正是 0.5.2 事故的形态，仅看进程发现不了）
  - cobian：timer active + 上次运行结果 + SMB 挂载在位（挂载丢失只报红不自愈，
    含凭据的挂载脚本留人工处理）
- **自愈 watchdog**：后台每 60s 探测，挂了自动尝试恢复——vector 未运行→`docker start`、
  僵尸态→`docker restart`、VL 挂→`docker start`、cobian timer 停→`systemctl start`、
  上次采集失败→触发重跑。组件级 10 分钟节流；所有自愈动作写留痕日志到 VL
  （`hostname=log-ui`，可搜索 `app_name:selfheal` 查看自愈历史）
- settings.yaml 加 `selfheal: false` 可整体关闭自愈（缺省开启）

## [0.5.4] - 2026-09-24

### Changed
- **纳管流程简化为单向**：纳管表 →「忽略」→ 忽略列表 →「删除」→ 回纳管表
  - 已注册主机操作列只留 `编辑 / 忽略`（移除「移除」按钮）
  - 被忽略的主机（含已注册）不再显示在纳管表，只出现在忽略列表；
    注册信息（IP/类型/备注）保留，从忽略列表删除后原样回到纳管表
  - 未注册主机删除忽略记录后，若仍在发日志将以「未注册」重新出现

## [0.5.3] - 2026-09-24

### Changed
- **已注册服务器也可忽略**：操作列新增「忽略」（原仅未注册主机可忽略），
  被忽略的注册主机保留在纳管表、状态显示「已忽略」（取代误导性的「静默 0 条」），
  可「取消忽略」恢复；忽略时自动带走登记的 IP/类型到忽略列表
- 忽略列表操作「恢复采集」更名「删除」（语义即删除该忽略记录；
  主机若仍在发日志会重新出现在纳管表，未注册）
- 全站文字提亮：暗色主题 text/text2/muted 各提一档，亮色主题 muted 加深一档

## [0.5.2] - 2026-09-24

### Changed
- **忽略 = 拒收**：被忽略主机的日志不再入库（此前仅 UI 不显示、数据照收）。
  忽略/恢复后自动改写 Vector 受管块并热重载（SIGHUP），秒级生效；
  写前备份 + `vector validate` 校验，失败自动回滚，采集不中断
- 忽略列表整理为表格：名称 / IP / 类型 / 恢复采集（原为纯文本堆叠）；
  名称本身是 IP 的自动填入 IP 列
- 忽略按钮增加确认提示（操作语义从"隐藏"升级为"拒收"，防手滑）
- `ignored` 配置结构升级为 `{name, ip, type}`，老格式读取时自动兼容，
  无需手工迁移

### Added
- `app/vecsync.py`：忽略清单 → vector.toml 受管块同步器（含回滚护栏）
- Cobian 采集器（宿主脚本）读忽略清单，`192.168.0.28` 被忽略时跳过采集
  （Cobian 路径绕过 Vector，需单独设闸）
- 首次接线时理顺 sink inputs：生产曾把 `rclone_prep` 直连 sink（与归一化链
  双路消费，rclone 恢复发日志时会重复入库）；现统一经归一化链，消除该潜伏问题

### Fixed
- **修复首次上线导致的 Vector 中断（约 2 分钟，已恢复）**：condition 误用
  `contains()`（字符串子串查找，传数组运行时报 E110）；正确为数组成员检查
  `includes()`。且 `vector validate` 未拦截该运行时错误、SIGHUP 热重载触发
  Vector 0.54 的 fanout panic。加固：改用 `docker restart` 全新加载（中断数秒
  但可靠），重启后健康检查（容器运行 + 无致命日志），任一环节失败自动回滚
  备份并再次重启

## [0.5.1] - 2026-09-24

### Fixed
- **页脚版本号被冻结**：`version` 在模块 import 时只取一次值，更新 CHANGELOG 后页脚仍显示旧版本，
  与实时的 `/version`、`/changelog` 三处互相矛盾。改为渲染时读取，升版本不必再重启进程
- `scripts/version.sh` 进位规则：每段 0-9、逢 10 进位（0.0.9→0.1.0、0.9.9→1.0.0），
  此前会产出 0.5.10 这类版本号

### Added
- `scripts/pre-commit` 钩子：改动 `app/`/`templates/`/`static/` 却未一并提交 `CHANGELOG.md` 时
  拦截提醒（确无需记录时用 `git commit --no-verify` 跳过）

## [0.5.0] - 2026-08-05

### Added
- CHANGELOG.md 版本记录（本文件）
- UI 显示版本号（页脚，读自 CHANGELOG 最新版本）
- 服务器页：添加/注册按钮（点击展开表单）
- 服务器页：行序号列
- 自动升级版本脚本 `scripts/version.sh`

### Fixed
- **SSO 登录死循环**：overview 路由丢失 ticket 参数（refactor 时），yz-login 跳回 `/` 无法建会话

## [0.4.0] - 2026-08-05

### Added
- Vector `level_normalize`：err/crit/alert/emerg→error、warn→warning 等级归一化
- Vector `journald_filter`：journald（OpenEuler-Log-AI）只保留 warning+
- 总览大屏：自动发现新主机（不仅已注册）、0 错误服务器也显示

### Fixed
- VRL 语法：`drop()` 不存在、`to_string()` fallible
- 总览 nhosts tile 使用错误数据源（field_values→stats_by）

## [0.3.0] - 2026-08-05

### Added
- /search 服务器下拉与 /servers 同步（过滤已忽略、注册优先）
- 下拉/等级/条数切换自动刷新（无需点搜索）
- Cobian 采集器 hostname 固定为 192.168.0.28（不按源 IP 散开）

## [0.2.0] - 2026-08-04

### Added
- 服务器纳管：编辑/登记/忽略/恢复、状态指示灯、点击名称查日志
- CSS/JS 分离到 static/
- 工业监控风设计系统（深石板、语义色、状态呼吸灯、tile 顶条）
- Docker 部署方案 stack/（compose+vector+provisioning+cobian+DEPLOY.md）

### Fixed
- Code review：静态文件入库、XSS 转义、vl.query_logs 容错、事件循环阻塞（def 路由）、N+1 last_seen（host_summary 批量）、CSRF Origin 校验

## [0.1.0] - 2026-08-04

### Added
- 初始版本：yz-login SSO、总览大屏（ECharts）、日志搜索（自动刷新）、实时 tail（WebSocket）、个人偏好设置、管理员 SSO 配置
- systemd 部署（端口 80）
