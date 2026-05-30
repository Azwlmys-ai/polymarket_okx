# VPS 运维手册 — polymarket_okx

> 生成时间：2026-05-26  
> 维护者：Claude (Cowork)  
> 严禁写入：VPS 密码、API Key、私钥明文

---

## 基本信息

| 项目 | 值 |
|------|-----|
| VPS IP | 158.247.220.86 |
| Hostname | okx-seoul |
| OS | Ubuntu 24.04.4 LTS |
| Kernel | 6.8.0-117-generic |
| SSH User | root |
| SSH Alias | `vps-polymarket`（见 `.ssh/config`） |
| 项目路径 | `/opt/polymarket_okx` |

---

## SSH 配置

### 本地 `.ssh/config`（workspace 内）

```
Host vps-polymarket
    HostName 158.247.220.86
    User root
    IdentityFile ~/.ssh/cowork_id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    StrictHostKeyChecking accept-new
    ConnectTimeout 10
```

密钥文件路径（本地 workspace）：`polymarket_okx/.ssh/cowork_id_ed25519`（已加入 `.gitignore`）

**直连方式（本地终端）：**
```bash
ssh root@158.247.220.86          # 直连（用本机 ~/.ssh/id_ed25519）
ssh vps-polymarket               # alias（用 cowork key）
```

> ⛔ 严禁：不得在任何文件、脚本、日志、chat 中保存 VPS 明文密码

### ✅ 当前安全状态（已加固，2026-05-26）

| 项目 | 状态 |
|------|------|
| PubkeyAuthentication | ✅ 启用（默认） |
| PasswordAuthentication | ✅ **no**（已关闭） |
| 本机 key | `~/.ssh/id_ed25519`（已在 VPS authorized_keys） |
| Cowork key | `polymarket_okx/.ssh/cowork_id_ed25519`（已在 VPS authorized_keys） |
| 授权密钥数量 | 3 个（libo 本机 + cowork + 原有 1 个） |
| 配置文件 | `/etc/ssh/sshd_config.d/50-cloud-init.conf`（已备份） |

> 加固流程：`scripts/secure_ssh.sh` 全程 9 步通过，`sshd -T` 验证确认。

---

## Systemd Services

| Service | 状态 | 说明 |
|---------|------|------|
| `polymarket-okx-anchor.service` | ✅ running | 主 paper anchor 仿真，**禁止随意重启** |
| `polymarket-health-check.timer` | ✅ active | 每 5 分钟健康检查，输出到 research/ |
| `polymarket-health-check.service` | oneshot | 由 timer 触发 |

### 查看主服务状态
```bash
ssh vps-polymarket 'systemctl status polymarket-okx-anchor.service --no-pager -l | tail -20'
```

### 查看实时日志
```bash
ssh vps-polymarket 'journalctl -u polymarket-okx-anchor.service -f'
```

---

## Tmux Sessions

| Session 名 | 内容 | 说明 |
|------------|------|------|
| `shadow_follow` | shadow_execution_recorder.py | 影子执行跟踪，`--threshold 130`，仅 dist>130 时写入，长静默期属正常 |

### 常用 tmux 操作
```bash
# 列出 sessions
ssh vps-polymarket 'tmux ls'

# 查看 shadow_follow 内容（不 attach）
ssh vps-polymarket 'tmux capture-pane -t shadow_follow -p | tail -30'

# 进入 session（本地 ssh 后）
ssh vps-polymarket
tmux attach -t shadow_follow

# 重启 shadow_follow（仅在确认 stale 后）
ssh vps-polymarket 'tmux kill-session -t shadow_follow; cd /opt/polymarket_okx && tmux new-session -d -s shadow_follow "source .venv/bin/activate && python research/shadow_execution_recorder.py"'
```

---

## 关键文件路径

| 文件 | 路径 | 说明 |
|------|------|------|
| 主服务脚本 | `/opt/polymarket_okx/research/paper_anchor_sim.py` | 由 systemd 管理，勿手动运行 |
| 影子执行脚本 | `/opt/polymarket_okx/research/shadow_execution_recorder.py` | tmux shadow_follow 内运行 |
| 健康检查脚本 | `/opt/polymarket_okx/research/vps_health_check.py` | 每 5 分钟由 timer 执行 |
| 信号事件文件 | `/opt/polymarket_okx/research/paper_anchor_signal_events.jsonl` | 主服务写入 |
| 影子执行事件 | `/opt/polymarket_okx/research/shadow_execution_events.jsonl` | shadow_follow 写入，threshold=130，稀疏正常 |
| shadow follow 日志 | `/opt/polymarket_okx/research/shadow_execution_follow.log` | recorder stdout，capture-pane 不可见属正常 |
| 健康报告 | `/opt/polymarket_okx/research/vps_health_report.md` | 每 5 分钟更新 |
| 环境变量 | `/opt/polymarket_okx/.env` | 禁止 git 追踪 |

---

## Healthcheck 路径

```bash
# 快速健康状态（看最新报告）
ssh vps-polymarket 'cat /opt/polymarket_okx/research/vps_health_report.md'

# 信号事件最新 5 条
ssh vps-polymarket 'tail -5 /opt/polymarket_okx/research/paper_anchor_signal_events.jsonl | python3 -c "import sys,json; [print(json.loads(l)[\"ts\"], json.loads(l)[\"event_type\"], json.loads(l).get(\"slug\",\"\")) for l in sys.stdin]"'

# 影子事件最新 3 条（含 fallback 状态）
ssh vps-polymarket 'tail -3 /opt/polymarket_okx/research/shadow_execution_events.jsonl | python3 -c "import sys,json; [print(json.loads(l)[\"ts_utc\"], \"fallback=\"+str(json.loads(l).get(\"fallback_used\")), json.loads(l).get(\"reject_reason\",\"\")) for l in sys.stdin]"'

# 全量基线巡检
ssh vps-polymarket 'bash /opt/polymarket_okx/scripts/vps_baseline_check.sh'
```

---

## 常用巡检命令速查

```bash
# 系统负载
ssh vps-polymarket 'uptime && free -m && df -h /'

# 主服务 journal 最后 20 行
ssh vps-polymarket 'journalctl -u polymarket-okx-anchor.service -n 20 --no-pager'

# 检查 fallback 比例（最近 50 条影子事件）
ssh vps-polymarket 'tail -50 /opt/polymarket_okx/research/shadow_execution_events.jsonl | python3 -c "import sys,json,collections; d=[json.loads(l) for l in sys.stdin]; c=collections.Counter(x.get(\"reject_reason\",\"unknown\") for x in d); [print(k,v) for k,v in c.items()]; fb=sum(1 for x in d if x.get(\"fallback_used\")); print(f\"fallback_used=true: {fb}/{len(d)} = {fb/len(d)*100:.1f}%\")"'

# tmux 快照
ssh vps-polymarket 'tmux ls && tmux capture-pane -t shadow_follow -p 2>/dev/null | tail -15'

# timer 下次触发时间
ssh vps-polymarket 'systemctl list-timers polymarket-health-check.timer'
```

---

## 系统资源基线（2026-05-26）

| 项目 | 值 |
|------|-----|
| vCPU | 1 × Intel Xeon Skylake |
| RAM | 955 MB（used 382M / avail 573M） |
| Swap | 2.4 GB（used 23M） |
| Disk `/` | 23G 总量，6.3G 已用（29%）|
| Python | 3.12.3（/usr/bin/python3） |
| venv | `/opt/polymarket_okx/.venv` ✅ |

---

## 操作禁令

- ❌ 不重启 `polymarket-okx-anchor.service`（除非明确指令）
- ❌ 不 kill 主服务进程（PID 34640 或后继）
- ❌ 不进入真实交易模式
- ❌ 不修改交易策略参数
- ❌ 不把 `.env` 或任何密码写入 git

---

## 运维日志（append-only）

| 日期 | 操作 | 操作人 | 结果 |
|------|------|--------|------|
| 2026-05-26 08:53 | 初始化运维基线，SSH key 部署，健康检查 | Claude (Cowork) | PARTIAL，SSH 密码登录待关闭，shadow_follow stale 待确认 |
| 2026-05-26 17:12 | SSH 安全加固：`secure_ssh.sh` 9 步全通过 | Claude (Cowork) | ✅ PasswordAuthentication=no，免密登录验证通过（FINAL_OK） |
| 2026-05-26 17:16 | shadow_follow 断写诊断 | Claude (Cowork) | ✅ 进程正常，断写为误报：threshold=130 导致稀疏写入，无需重启 |
| 2026-05-26 17:22 | vps_health_check.py 修正 shadow stale 误报 | Claude (Cowork) | ✅ 新增 IDLE_OK 逻辑 + get_shadow_threshold() + 5/5 自测通过，部署后状态 HEALTHY |
