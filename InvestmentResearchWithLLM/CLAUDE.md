# InvestmentResearchWithLLM — CLAUDE.md

## 项目定位

基于 DeepSeek R1 + Tavily 搜索的**行业投研助手**，自动输出产业链图谱、公司定位分析、持仓研究报告。

- 端口：**8002**（finance-analysis 8000，holderAndAction 8001，本项目 8002）
- 服务器：`101.201.171.174`，systemd 服务名 `investment-research`
- 本项目**只读** holderAndAction 的 `trading.db`，不写入其他项目数据库

---

## 目录结构

```
InvestmentResearchWithLLM/
├── main.py                  # FastAPI 入口，port 8002
├── orchestrator.py          # 意图识别（deepseek-v4-pro）+ 路由
├── chain_analyzer.py        # 产业链分析，调 deepseek-reasoner，带 24h 缓存
├── company_analyzer.py      # 公司分析，调 deepseek-reasoner，带 6h 缓存
├── portfolio_research.py    # 读 holderAndAction 持仓 → 组合研究报告，带 1h 缓存
├── data_fetcher.py          # Tavily 搜索 + yfinance + AKShare 统一接口
├── report_generator.py      # 缓存读写（SQLite）+ Markdown 格式化（去 <think> 标签）
├── database.py              # SQLite，数据存 data/reports.db
├── models.py                # ReportCache 表（report_type/cache_key/content/expires_at）
├── routers/
│   ├── chat.py              # SSE 流式接口 POST /api/chat/stream
│   └── research.py          # REST 结构化接口 /api/research/*
├── prompts/
│   ├── chain_analysis.md    # 产业链分析 prompt 模板
│   ├── company_analysis.md  # 公司分析 prompt 模板
│   └── portfolio_research.md# 持仓研究 prompt 模板
├── static/
│   └── chat.html            # Chat UI（飞书蓝风格，marked.js 渲染）
├── data/
│   └── reports.db           # 报告缓存数据库
├── requirements.txt
├── .env.example
├── deploy.sh                # 一键部署脚本
└── 设计文档.md
```

---

## 环境变量

服务器上 `.env` 不带 `export` 前缀（systemd EnvironmentFile 不支持 export）：

```
DEEPSEEK_API_KEY=sk-xxx
TAVILY_API_KEY=tvly-xxx
HOLDER_DB_PATH=/opt/holder-action/data/trading.db
PORT=8002
```

本地开发时用 `source .env`，`.env` 可以带 `export`。

---

## 模型分工

| 用途 | 模型 | 原因 |
|------|------|------|
| 意图识别、通用 QA | `deepseek-v4-pro` | 轻量快速，节省费用 |
| 产业链/公司/持仓分析 | `deepseek-reasoner` | R1 推理链，报告质量更高 |

deepseek-reasoner 响应约 30-60 秒，max_tokens 按模块设置：chain=8000，company=4000，portfolio=6000。

---

## 缓存策略

| 报告类型 | TTL | cache_key |
|---------|-----|-----------|
| chain | 24h | 行业名称（如 "AI算力"） |
| company | 6h | ticker 大写（如 "NVDA"） |
| portfolio | 1h | "latest" |

缓存存在 `data/reports.db` 的 `reports` 表，过期自动失效。

---

## API 端点速查

```
GET  /api/health

POST /api/chat/stream              SSE 流式，body: {"message": "...", "session_id": "..."}

POST /api/research/chain           body: {"industry": "AI算力"}
POST /api/research/company         body: {"ticker": "NVDA"}
POST /api/research/portfolio       无 body，自动读 holderAndAction 持仓
GET  /api/research/reports         历史缓存列表，?type=chain&limit=10
```

---

## 意图路由逻辑

orchestrator 调 deepseek-v4-pro 识别意图，返回 `{"intent": "chain|company|portfolio|compare|qa", "entities": [...]}`：

- `chain` → ChainAnalyzer，entities[0] 为行业名
- `company` → CompanyAnalyzer，entities[0] 为 ticker/公司名
- `compare` → CompanyAnalyzer × 2，entities[0] 和 [1]
- `portfolio` → PortfolioResearch，无需 entities
- `qa` → deepseek-v4-pro 直接流式回答

---

## 数据来源与可信度

- **Tavily 搜索**：用于行业动态和新闻背景，不作为估值计算依据
- **yfinance**：市值/PE/毛利率/营收增速，报告中标注"仅供参考"
- **AKShare**：A 股个股基本信息（`data_fetcher.get_cn_stock`）
- **精确估值**：需在 Claude CLI 中运行 `/equity-research <ticker>`（IBES 共识数据，MCP 工具，服务端无法调用）

---

## 与现有系统的关系

| 项目 | 端口 | 职责 | 本项目交互 |
|------|------|------|-----------|
| finance-analysis | 8000 | LLM 信号生成 | 无 |
| holderAndAction | 8001 | 持仓管理 | 只读 `/opt/holder-action/data/trading.db` |
| InvestmentResearchWithLLM | 8002 | 行业投研 | — |

---

## 部署与运维

```bash
# 本地增量同步代码到服务器
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='data/' --exclude='.env' --exclude='.venv' \
  ./ root@101.201.171.174:/opt/investment-research/

# 重启服务
ssh root@101.201.171.174 'systemctl restart investment-research'

# 查看实时日志
ssh root@101.201.171.174 'journalctl -u investment-research -f'

# 完整一键部署（首次或大版本升级）
bash deploy.sh 101.201.171.174 root
```

---

## 常见问题

**报告返回 Internal Server Error**
→ 先查日志：`journalctl -u investment-research -n 40 --no-pager`
→ 最常见：`.env` 带了 `export` 前缀导致环境变量读不到

**产业链报告很慢（30-60s）**
→ 正常，deepseek-reasoner 有完整推理链。Chat UI 有流式输出，不会白屏。

**持仓报告显示"暂无开仓持仓"**
→ 检查 `HOLDER_DB_PATH` 是否指向正确路径，holderAndAction 是否有 status='open' 的持仓记录

**Prompt 调整**
→ 直接编辑 `prompts/*.md`，重启服务后生效，无需改 Python 代码
