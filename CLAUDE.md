# holderAndAction — 项目说明文档

## 项目定位

本项目是**大模型金融分析系统（`/opt/finance-analysis`）的持仓管理与调仓建议前台服务**。

核心功能：
1. **展示大模型金融分析系统的输出结果**（信号、扫描、宏观）
2. **手机截图上传 → Qwen-VL 解析持仓 → 存入数据库**
3. **基于持仓 + LLM 信号 → DeepSeek R1 生成调仓建议**

运行端口：**8001**（finance-analysis 用 8000，避免冲突）

---

## 目录结构

```
holderAndAction/
├── main.py                  # FastAPI 入口
├── database.py              # SQLite，数据存 data/trading.db
├── models.py                # ORM 模型（4 张表：positions/trades/orders/signals_cache）
├── schemas.py               # Pydantic v2 Schema（含截图解析/调仓建议新增 Schema）
├── image_parser.py          # Qwen-VL 截图解析（中文券商截图识别）
├── advisor.py               # DeepSeek R1 调仓建议生成
├── signal_reader.py         # 读取 finance-analysis 输出的 *_api_output.txt
├── price_fetcher.py         # yfinance 实时价格（5 分钟缓存）
├── sync.py                  # 持仓数据库 → data/portfolio.json 同步
├── routers/
│   ├── advisor.py           # 截图解析 + 调仓建议接口
│   ├── portfolio.py         # 持仓 CRUD + 平仓
│   ├── trades.py            # 交易历史 + 统计
│   ├── signals.py           # 读取/刷新 LLM 信号
│   ├── scan.py              # 市场扫描
│   └── dashboard.py         # 仪表盘 KPI + 宏观数据
├── static/
│   └── upload.html          # 手机截图上传页面（深色主题，移动端优化）
├── data/                    # 运行时数据目录（.gitignore）
│   ├── trading.db           # SQLite 数据库
│   └── portfolio.json       # 持仓同步文件
├── requirements.txt
├── .env.example             # 环境变量模板
└── deploy.sh                # 一键部署到阿里云 ECS
```

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FINANCE_PROJECT_ROOT` | finance-analysis 项目根目录 | `/opt/finance-analysis` |
| `QWEN_API_KEY` | 通义千问 API Key（截图解析） | 已内置 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（调仓建议） | 需手动填写 |
| `PORT` | 服务端口 | `8001` |

`.env` 格式（所有变量必须带 `export`）：
```bash
export FINANCE_PROJECT_ROOT=/opt/finance-analysis
export QWEN_API_KEY=sk-7f45108d8cd043c48306f860228d5479
export DEEPSEEK_API_KEY=sk-your-deepseek-key
export PORT=8001
```

---

## 访问地址

| 页面 | 地址 |
|------|------|
| 仪表盘 | `http://101.201.171.174:8001/` |
| 截图上传（手机） | `http://101.201.171.174:8001/upload` |
| API 文档 | `http://101.201.171.174:8001/docs` |

## API 端点速查

```
GET  /api/health                              健康检查

# 截图解析 & 调仓建议（新增）
POST /api/advisor/parse-screenshot            上传截图 → Qwen-VL 解析持仓
POST /api/advisor/advice                      传入持仓列表 → DeepSeek R1 建议
POST /api/advisor/advice-from-db             直接读数据库持仓 → DeepSeek R1 建议

# 持仓管理
GET  /api/portfolio/positions                 所有开仓（含实时价格 + P&L）
POST /api/portfolio/positions                 新建持仓
PUT  /api/portfolio/positions/{id}            更新止损/目标
DELETE /api/portfolio/positions/{id}          删除持仓
POST /api/portfolio/positions/{id}/close      平仓 → 自动生成 trade 记录

# 交易历史
GET  /api/trades                              交易记录列表（分页）
POST /api/trades                              手动录入
DELETE /api/trades/{id}                       删除
GET  /api/trades/stats                        统计（胜率/盈利因子）

# 信号（读取 finance-analysis 输出文件）
GET  /api/signals                             所有资产最新信号
GET  /api/signals/{asset}                     单资产信号详情
POST /api/signals/refresh/{asset}             后台触发重新分析

# 市场扫描
GET  /api/scan/latest                         最新扫描结果
POST /api/scan/run?group=quick                触发扫描

# 仪表盘
GET  /api/dashboard/summary                   持仓 KPI + 告警
GET  /api/dashboard/macro                     宏观数据（VIX/DXY/10Y）

# 手机上传页面
GET  /upload                                  持仓截图上传页面
```

---

## 核心模块说明

### `image_parser.py` — Qwen-VL 截图解析

- 模型：`qwen-vl-max`，支持富途、老虎、同花顺等中文券商截图
- 输入：图片二进制（JPG/PNG/WEBP，限 10MB）
- 输出：结构化持仓列表（asset/ticker/quantity/entry_price/current_price/pnl）
- 解析失败时返回 `success=false` + `error` 字段，不抛出异常

### `advisor.py` — DeepSeek R1 调仓建议

- 模型：`deepseek-reasoner`（R1，含 chain-of-thought）
- 输入：持仓列表 + finance-analysis 信号摘要（自动过滤 no_trade）
- 输出：`summary`（市场概述）+ `recommendations`（逐资产建议）+ `risk_notes`（风险提示）
- `raw_thinking` 字段包含 R1 的推理过程，可用于调试

### `signal_reader.py` — 信号文件读取

- `FINANCE_ROOT` 通过 `FINANCE_PROJECT_ROOT` 环境变量配置，本地/服务器均可运行
- 信号文件路径：`{FINANCE_ROOT}/outputs/{ticker}_api_output.txt`
- 解析顺序：直接 JSON → markdown 代码块 → 大括号匹配，兼容 DeepSeek R1 的 `<think>` 标签

---

## 部署

### 阿里云 ECS

```bash
# 本地执行，一键部署
cd ~/Desktop/holderAndAction
bash deploy.sh 101.201.171.174 root
```

部署后：
- 服务自动注册为 systemd 服务，随机器重启自动启动
- 手机上传页面：`http://101.201.171.174:8001/upload`
- API 文档：`http://101.201.171.174:8001/docs`

### 服务器常用命令

```bash
# 查看状态
systemctl status holder-action

# 查看日志
journalctl -u holder-action -f

# 重启服务
systemctl restart holder-action

# 手动启动（调试）
cd /opt/holder-action && source .env
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

### 本地开发

```bash
cd ~/Desktop/holderAndAction
pip install -r requirements.txt
cp .env.example .env   # 填写 DEEPSEEK_API_KEY
source .env
uvicorn main:app --reload --port 8001
```

---

## 与 finance-analysis 的关系

| 项目 | 路径 | 端口 | 职责 |
|------|------|------|------|
| finance-analysis | `/opt/finance-analysis` | 8000 | LLM 信号生成、回测、市场扫描 |
| holderAndAction | `/opt/holder-action` | 8001 | 持仓管理、截图解析、调仓建议 |

holderAndAction **只读** finance-analysis 的输出文件（`outputs/*.txt`，`market_scan_output.json`），不写入，两个项目完全解耦。

---

## 飞书推送时机

飞书推送由服务器 cron 驱动，在 finance-analysis 每日分析完成后 15 分钟推送：

| 时间 | 触发 | 内容 |
|------|------|------|
| 10:15 CST | 早盘分析后 | 读持仓 → DeepSeek R1 生成建议 → 推飞书 |
| 19:15 CST | 晚盘分析后 | 同上 |

日志文件：`/opt/holder-action/data/feishu.log`

需在 `.env` 中配置 `FEISHU_WEBHOOK_URL` 才会实际发送，否则打印错误跳过。

## 注意事项

- **阿里云安全组**：需开放 8001 端口入站规则，否则手机无法访问
- **Qwen-VL 费用**：约 ¥0.001/次（极低，无需担心）
- **DeepSeek R1 响应时间**：调仓建议约 15-30 秒（R1 有完整推理过程）
- **持仓数据库独立**：`data/trading.db` 与 finance-analysis 的 `trading.db` 是两个独立数据库，互不影响
