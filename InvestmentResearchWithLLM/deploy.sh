#!/bin/bash
# 一键部署 InvestmentResearchWithLLM 到阿里云 ECS
# 用法：bash deploy.sh [server_ip] [user]
# 示例：bash deploy.sh 101.201.171.174 root

SERVER=${1:-"101.201.171.174"}
USER=${2:-"root"}
REMOTE_DIR="/opt/investment-research"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "================================================"
echo " InvestmentResearchWithLLM 部署脚本"
echo " 目标服务器: $USER@$SERVER"
echo " 远程目录:   $REMOTE_DIR"
echo " 端口:       8002"
echo "================================================"
echo ""
echo "⚠️  部署前请确认阿里云安全组已开放入站端口 8002"
echo ""
read -p "已确认，继续部署？[y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "已取消"
  exit 0
fi

echo ""
echo "==> [1/3] 同步代码..."
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='data/' --exclude='.env' --exclude='.venv' \
  --exclude='*.jpg' --exclude='*.png' \
  "$LOCAL_DIR/" "$USER@$SERVER:$REMOTE_DIR/"

echo ""
echo "==> [2/3] 服务器端初始化..."
ssh "$USER@$SERVER" bash <<ENDSSH
set -e
cd $REMOTE_DIR

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

# .env 初始化
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

# 写入 API Keys
sed -i 's|export DEEPSEEK_API_KEY=.*|export DEEPSEEK_API_KEY=sk-1a9d3723d20446058797e46f3e829d90|' .env
sed -i 's|export TAVILY_API_KEY=.*|export TAVILY_API_KEY=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|' .env
sed -i 's|export HOLDER_DB_PATH=.*|export HOLDER_DB_PATH=/opt/holder-action/data/trading.db|' .env
grep -q 'FMP_API_KEY' .env || echo 'export FMP_API_KEY=3euHIX1gvPaTpAdDPmdxiSBUHkveDAG2' >> .env

echo "当前 .env（已脱敏）："
grep -v "API_KEY" .env || true
ENDSSH

echo ""
echo "==> [3/3] 注册 systemd 服务..."
ssh "$USER@$SERVER" bash <<'ENDSSH'
set -e

cat > /etc/systemd/system/investment-research.service <<'SERVICE'
[Unit]
Description=Investment Research LLM API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/investment-research
EnvironmentFile=/opt/investment-research/.env
ExecStart=/opt/investment-research/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8002
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable investment-research
systemctl restart investment-research
sleep 2

if systemctl is-active --quiet investment-research; then
  echo "✅ investment-research 服务已启动"
else
  echo "❌ 服务启动失败，查看日志："
  journalctl -u investment-research -n 30 --no-pager
  exit 1
fi

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "101.201.171.174")
echo ""
echo "================================================"
echo " ✅ 部署完成！"
echo ""
echo "   Chat UI：   http://${PUBLIC_IP}:8002/"
echo "   API 文档：  http://${PUBLIC_IP}:8002/docs"
echo "   健康检查：  http://${PUBLIC_IP}:8002/api/health"
echo "================================================"
ENDSSH
