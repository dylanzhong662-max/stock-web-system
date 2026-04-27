#!/bin/bash
# 一键部署 holderAndAction 到阿里云 ECS
# 用法：bash deploy.sh [server_ip] [user]
# 示例：bash deploy.sh 101.201.171.174 root

SERVER=${1:-"101.201.171.174"}
USER=${2:-"root"}
REMOTE_DIR="/opt/holder-action"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "================================================"
echo " holderAndAction 部署脚本"
echo " 目标服务器: $USER@$SERVER"
echo " 远程目录:   $REMOTE_DIR"
echo " 端口:       8001 (与 finance-analysis 8000 不冲突)"
echo "================================================"
echo ""
echo "⚠️  部署前请确认阿里云安全组已开放入站端口 8001"
echo "   控制台 → ECS → 安全组 → 入方向 → 端口 8001 / 来源 0.0.0.0/0"
echo ""
read -p "已确认安全组配置，继续部署？[y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "已取消"
  exit 0
fi

echo ""
echo "==> [1/3] 同步代码..."
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='data/' --exclude='.env' \
  "$LOCAL_DIR/" "$USER@$SERVER:$REMOTE_DIR/"

echo ""
echo "==> [2/3] 服务器端初始化..."
ssh "$USER@$SERVER" bash <<'ENDSSH'
set -e
cd /opt/holder-action

# 虚拟环境
if [ ! -d ".venv" ]; then
  echo "创建 Python 虚拟环境..."
  python3 -m venv .venv
fi

# 安装依赖
echo "安装 Python 依赖..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 数据目录
mkdir -p data

# .env 初始化（已存在则不覆盖）
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

# 写入 API Keys 和 Webhook（确保 key 正确）
# LLM_API_KEY 优先；DEEPSEEK_API_KEY 作为兼容回退
if grep -q "LLM_API_KEY" .env; then
  sed -i 's|export LLM_API_KEY=.*|export LLM_API_KEY=sk-6BV9Xfa9AJ09pkt0AHFPQtZUtlM28pCOnon6ArdIJW1fVyDP|' .env
else
  echo 'export LLM_API_KEY=sk-6BV9Xfa9AJ09pkt0AHFPQtZUtlM28pCOnon6ArdIJW1fVyDP' >> .env
fi
sed -i 's|export DEEPSEEK_API_KEY=.*|export DEEPSEEK_API_KEY=sk-c75ee0f98c8c410ea0e5005f3b3bc5fa|' .env
sed -i 's|export FEISHU_WEBHOOK_URL=.*|export FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/1b0f0064-48cb-43b5-9bb1-8b30188a3e8c|' .env

echo "当前 .env 内容（已脱敏）："
grep -v "API_KEY" .env || true
ENDSSH

echo ""
echo "==> [3/3] 注册 systemd 服务 + 飞书推送 cron..."
ssh "$USER@$SERVER" bash <<'ENDSSH'
set -e

# systemd 服务
cat > /etc/systemd/system/holder-action.service <<'SERVICE'
[Unit]
Description=HolderAction Portfolio API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/holder-action
EnvironmentFile=/opt/holder-action/.env
ExecStart=/opt/holder-action/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable holder-action
systemctl restart holder-action
sleep 2

# 检查服务状态
if systemctl is-active --quiet holder-action; then
  echo "✅ holder-action 服务已启动"
else
  echo "❌ 服务启动失败，查看日志："
  journalctl -u holder-action -n 20 --no-pager
  exit 1
fi

# 飞书推送 cron —— 在每日金融分析（10:00 和 19:00）运行后 15 分钟推送
# finance-analysis 的 run_daily.sh 在 10:00 / 19:00 运行
# 给分析留 15 分钟，10:15 / 19:15 推送调仓建议
CRON_LINE_MORNING="15 10 * * * source /opt/holder-action/.env && /opt/holder-action/.venv/bin/python3 /opt/holder-action/feishu_notifier.py >> /opt/holder-action/data/feishu.log 2>&1"
CRON_LINE_EVENING="15 19 * * * source /opt/holder-action/.env && /opt/holder-action/.venv/bin/python3 /opt/holder-action/feishu_notifier.py >> /opt/holder-action/data/feishu.log 2>&1"

# 避免重复写入
(crontab -l 2>/dev/null | grep -v "holder-action/feishu_notifier"; echo "$CRON_LINE_MORNING"; echo "$CRON_LINE_EVENING") | crontab -

echo "✅ 飞书推送 cron 已注册："
echo "   10:15 CST — 早盘分析后推送"
echo "   19:15 CST — 晚盘分析后推送"
echo ""
echo "当前 crontab（holder-action 相关）："
crontab -l | grep holder-action

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "SERVER_IP")
echo ""
echo "================================================"
echo " ✅ 部署完成！"
echo ""
echo "   仪表盘：    http://${PUBLIC_IP}:8001/"
echo "   截图上传：  http://${PUBLIC_IP}:8001/upload"
echo "   API 文档：  http://${PUBLIC_IP}:8001/docs"
echo ""
echo "   飞书 Webhook 已配置，10:15/19:15 自动推送调仓建议"
echo "================================================"
ENDSSH
