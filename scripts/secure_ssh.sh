#!/usr/bin/env bash
# secure_ssh.sh — SSH 安全加固：key 登录验证 + 关闭密码登录
# 在本地 Mac 执行，远程操作 VPS 158.247.220.86
#
# 安全门控：
#   - 步骤 4 免密验证失败 → 中止，不修改 sshd 配置
#   - 步骤 8 配置验证失败 → 报错，保留现场
#   - 任何步骤失败 → 立即 exit 1，不继续

set -euo pipefail

VPS="root@158.247.220.86"
KEY_FILE="$HOME/.ssh/id_ed25519"
KEY_COMMENT="polymarket_okx_vps"
SSHD_CONF="/etc/ssh/sshd_config.d/50-cloud-init.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  ✅ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠️  $*${NC}"; }
fail() { echo -e "${RED}  ❌ $*${NC}"; exit 1; }
step() { echo -e "\n── [Step $1] $2"; }

echo "════════════════════════════════════════"
echo "  SSH 安全加固  —  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Target: $VPS"
echo "════════════════════════════════════════"

# ── Step 1: 检查本机 ed25519 key ──────────────────
step 1 "检查本机 ed25519 key"
if [ -f "${KEY_FILE}.pub" ]; then
    ok "已存在：${KEY_FILE}.pub"
    echo "     $(cat ${KEY_FILE}.pub | cut -c1-72)..."
else
    warn "不存在 ${KEY_FILE}.pub，进入 Step 2 生成"

    # ── Step 2: 生成 key ──────────────────────────
    step 2 "生成 ed25519 key"
    ssh-keygen -t ed25519 -C "$KEY_COMMENT" -f "$KEY_FILE"
    ok "已生成：$KEY_FILE"
fi

# ── Step 3: 安装公钥到 VPS ────────────────────────
step 3 "安装公钥到 VPS（ssh-copy-id）"
echo "     → 将提示 VPS 密码，这是最后一次需要密码"
if ssh-copy-id -i "${KEY_FILE}.pub" "$VPS"; then
    ok "公钥已安装到 VPS authorized_keys"
else
    fail "ssh-copy-id 失败，请检查密码或网络"
fi

# ── Step 4: 验证免密登录 ──────────────────────────
step 4 "验证免密登录（关键验证门）"
RESULT=$(ssh -o PasswordAuthentication=no -o BatchMode=yes -o ConnectTimeout=8 \
    "$VPS" 'echo SSH_OK' 2>&1)

if [ "$RESULT" = "SSH_OK" ]; then
    ok "免密登录验证通过：$RESULT"
else
    fail "免密登录失败（输出：$RESULT）\n     → 中止操作，不修改 sshd 配置"
fi

# ── Step 5 & 6: 修改 sshd 配置 ───────────────────
step 5 "修改 VPS sshd 配置（PasswordAuthentication no）"
ssh -o BatchMode=yes "$VPS" bash << REMOTE
set -euo pipefail

CONF="$SSHD_CONF"

# 备份原文件
cp "\$CONF" "\${CONF}.bak_\$(date +%Y%m%d_%H%M%S)"
echo "  备份已创建"

# 修改
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' "\$CONF"
echo "  配置已修改"

# 确认修改生效
grep "PasswordAuthentication" "\$CONF"
REMOTE

ok "sshd 配置修改完成"

# ── Step 7: reload sshd ───────────────────────────
step 7 "Reload sshd（不中断现有连接）"
ssh -o BatchMode=yes "$VPS" 'systemctl reload sshd && echo RELOAD_OK'
ok "sshd reload 完成"

# ── Step 8: 验证配置生效 ──────────────────────────
step 8 "验证 PasswordAuthentication 状态"
PA_STATUS=$(ssh -o BatchMode=yes "$VPS" "sshd -T | grep ^passwordauthentication")
echo "     → $PA_STATUS"

if echo "$PA_STATUS" | grep -q "passwordauthentication no"; then
    ok "PasswordAuthentication = no ✓"
else
    fail "配置未生效（$PA_STATUS）\n     → 请手动检查 $SSHD_CONF"
fi

# ── Step 9: 最终验证 ──────────────────────────────
step 9 "最终免密登录验证"
FINAL=$(ssh -o PasswordAuthentication=no -o BatchMode=yes -o ConnectTimeout=8 \
    "$VPS" 'echo FINAL_OK' 2>&1)

if [ "$FINAL" = "FINAL_OK" ]; then
    ok "最终验证通过：$FINAL"
else
    fail "最终验证失败（$FINAL）"
fi

echo ""
echo "════════════════════════════════════════"
echo -e "${GREEN}  ✅ SSH 安全加固完成${NC}"
echo "  SSH key 登录：正常"
echo "  PasswordAuthentication：已关闭"
echo "  完全免密登录：已验证"
echo "════════════════════════════════════════"
