# InvestmentResearchWithLLM

基于 DeepSeek R1 + 多源数据的**行业投研助手**，自动输出产业链图谱、公司定位分析、持仓研究报告，并提供量化策略验证与交易行为诊断。

## 系统架构

```
┌───────────────────────────────────────────────────────────────────────┐
│                        Frontend (Static HTML)                         │
│                  chat.html (SSE Chat UI)  │  trades.html              │
└──────────────────────────────┬────────────────────────────────────────┘
                               │ HTTP / SSE
┌──────────────────────────────▼────────────────────────────────────────┐
│                         FastAPI (port 8002)                           │
│   main.py → routers/chat.py │ routers/research.py │ routers/trades.py│
└──────────┬───────────────────┼─────────────────────┼──────────────────┘
           │                   │                     │
┌──────────▼──────┐ ┌─────────▼──────────┐ ┌───────▼──────────┐
│  Orchestrator   │ │  Research Router    │ │  Trades Router   │
│  (意图识别路由)  │ │  (REST 结构化接口)  │ │  (CSV 解析诊断)  │
└──────┬──────────┘ └────────────────────-┘ └──────────────────┘
       │
       ├──→ ChainAnalyzer      (产业链分析)
       ├──→ CompanyAnalyzer    (公司分析)
       ├──→ PortfolioResearch  (持仓研究)
       └──→ QA Stream          (通用问答)
                    │
    ┌───────────────┼───────────────────────────────────┐
    │               │                                   │
┌───▼───┐    ┌──────▼───────┐    ┌─────────────────────▼─────┐
│  LLM  │    │  Data Layer  │    │    Quant & Validation     │
│       │    │              │    │                           │
│DeepSeek│   │ Tavily Search│    │ predictions.py            │
│ R1/V4 │   │ yfinance     │    │ prediction_analytics.py   │
│Qwen3  │   │ FMP API      │    │ backtest_simulation.py    │
│Gemini │   │ AKShare      │    │ parameter_sensitivity.py  │
│GPT-4  │   │ RAG (news)   │    │ consistency_test.py       │
│Claude │   │ Kenneth French│    │ neglect_weight_optimizer  │
└───────┘   └──────────────┘    │ scaling_advisor.py        │
                                │ transaction_costs.py      │
                                │ watchlist.py              │
                                └───────────────────────────┘
```

## 核心功能

### 1. 产业链分析 (ChainAnalyzer)

输入一个行业关键词（如"AI算力"），自动完成：
- Tavily 中英文双语搜索行业动态（各10+5条）
- RAG 语义检索近7天行业新闻/财报/SEC Filing
- 从搜索结果自动提取 ticker → yfinance 拉取实时财务数据
- International Neglect-Alpha Screener：全球市场（US/JP/KR/EU）低覆盖高增长股筛选
- DeepSeek R1 生成完整产业链报告（层级划分、竞争格局、投资机会）
- 自动提取方向性预测存入 Prediction 表，后续可验证命中率

**缓存策略**：24小时 TTL，同主题+同模型才命中

### 2. 公司分析 (CompanyAnalyzer)

输入 ticker 或公司名，自动完成：
- FMP + yfinance 获取市值/PE/毛利率/营收增速等核心指标
- RAG 检索公司相关新闻（earnings/SEC Filing/insider）
- Tavily 补充中文财报动态
- DeepSeek R1 生成公司定位分析报告

**缓存策略**：6小时 TTL

### 3. 持仓研究 (PortfolioResearch)

自动读取 holderAndAction 的 `trading.db`（只读），完成：
- 每个持仓获取实时价格、计算浮盈
- 市值加权 Beta 计算（R²<0.1 的持仓降权50%）
- Fama-French 4因子回归（Mkt/SMB/HML/UMD，Newey-West HAC 标准误）
- 持仓间相关性矩阵（含尾部风险相关性：SPY底部10%下跌日）
- ATR 止损位计算（14日 ATR）
- 盈利加仓评估（ATR 倒金字塔体系）
- 交易成本估算（佣金+价差+市场冲击）
- 监控清单注入（上一次分析提取的关键变量）
- DeepSeek R1 生成组合研究报告

**缓存策略**：1小时 TTL

### 4. 交易行为诊断 (TradeAnalytics)

上传嘉信证券(Schwab) CSV 交易记录：
- 自动解析 Buy/Sell 交易
- FIFO 匹配生成完整 Round Trip
- 计算胜率/盈亏比/Profit Factor/期望值/最大回撤/连续亏损
- 按标的分组统计
- DeepSeek R1 生成交易行为诊断报告（截断利润？过度交易？）

### 5. 盈利加仓顾问 (ScalingAdvisor)

基于 ATR + 倒金字塔规则自动评估所有开仓持仓：
- 条件1：浮盈 ≥ 1×ATR
- 条件2：价格 > 5日均线
- 条件3：无放量长上影线（抛压检测）
- 条件4：建仓 < 5天（动量窗口）
- 加仓比例 5:3:2 倒金字塔，止损必须上移

### 6. 量化验证体系

| 模块 | 功能 |
|------|------|
| `predictions.py` | 从报告中提取方向性判断，到期后自动结算 |
| `prediction_analytics.py` | Confidence 校准(Brier Score) + Walk-Forward + IC Decay |
| `backtest_simulation.py` | 月度筛选 → 持有N天 → 超额收益(含交易成本) |
| `parameter_sensitivity.py` | Neglect Score 各阈值 ±30% 敏感性分析 |
| `consistency_test.py` | 同数据N次运行，比较预测方向一致性 |
| `neglect_weight_optimizer.py` | IC-weighted 数据驱动 vs 学术先验权重 |

### 7. 监控清单系统 (Watchlist)

- 持仓分析报告生成后，自动从报告中提取结构化监控项
- 支持手动添加/停用监控项
- 支持设置 bullish/bearish 阈值触发条件
- 下次分析时将监控清单注入 prompt，让 LLM 评估变化
- 可配合飞书 webhook 推送预警

## 意图路由

用户输入通过 Orchestrator 的轻量模型（deepseek-v4-pro / qwen3.6-flash）识别意图：

| 意图 | 触发示例 | 路由到 |
|------|---------|--------|
| `chain` | "分析AI算力产业链" | ChainAnalyzer |
| `company` | "分析英伟达"、"NVDA怎么样" | CompanyAnalyzer |
| `compare` | "比较NVDA和AMD" | CompanyAnalyzer × 2 |
| `portfolio` | "我的持仓怎么样" | PortfolioResearch |
| `qa` | 其他通用问题 | 直接LLM流式回答(+RAG) |

## 多模型支持

通过 `llm_client.py` 统一管理，支持动态切换：

| 模型 | 用途 | API 路由 |
|------|------|---------|
| DeepSeek R1 (deepseek-reasoner) | 深度分析报告（默认） | 原生 API |
| DeepSeek V4 Pro | 意图识别 / 快速回答 | 原生 API |
| Qwen3.7 Max/Plus/Flash | 推理/均衡/快速 | DashScope API |
| QwQ Plus | 推理模型 | DashScope API |
| Gemini 3.1/3/2.5 Pro | 备选分析 | CloseAI 代理 |
| GPT-4.1 / GPT-4o / o3 | 备选 | CloseAI 代理 |
| Claude Sonnet 4.6 | 备选 | CloseAI 代理 |

## 数据来源

| 数据源 | 用途 | 接入方式 |
|--------|------|---------|
| Tavily | 行业新闻搜索 | REST API |
| yfinance | 股票基本面/价格 | Python SDK |
| FMP (Financial Modeling Prep) | OHLC/财务/Screener | REST API |
| AKShare | A股数据/美股日线 | Python SDK |
| Kenneth French | Fama-French 因子 | CSV 下载(含ETF代理fallback) |
| RAG News System | 语义新闻检索/信号/风险overlay | 自建服务 API |
| holderAndAction DB | 当前持仓数据 | SQLite 只读 |

## 数据层架构

`data_fetcher.py` 是兼容层，实际拆分为 `data_providers/` 包：

```
data_providers/
├── ticker_utils.py     # ticker 格式转换 (FMP/AV/yfinance)
├── cache.py            # SQLite + 内存双层缓存
├── financial_data.py   # 批量/单只财务快照
├── price_series.py     # 日线序列 + FF因子下载
├── quant.py            # Beta/多因子回归/相关性/ATR
├── intl_screener.py    # 国际neglect-alpha筛选
└── search.py           # Tavily搜索封装
```

## API 端点

### Chat (SSE 流式)
```
POST /api/chat/stream     body: {"message": "...", "model": "deepseek-reasoner"}
```

### Research (REST)
```
POST /api/research/chain           产业链分析
POST /api/research/company         公司分析
POST /api/research/portfolio       持仓研究
GET  /api/research/reports         缓存报告列表

GET  /api/research/predictions              预测记录
GET  /api/research/predictions/performance  命中率统计
POST /api/research/predictions/resolve      手动结算
GET  /api/research/predictions/analytics    校准+WF+IC分析
GET  /api/research/predictions/calibration  Confidence校准

GET  /api/research/watchlist       监控清单
POST /api/research/watchlist       添加监控项
DELETE /api/research/watchlist/{id} 停用
PUT  /api/research/watchlist/{id}/value 更新观测值
POST /api/research/watchlist/check-alerts  阈值检查+飞书推送

POST /api/research/backtest        Walk-Forward回测
POST /api/research/sensitivity     参数敏感性分析
POST /api/research/consistency     一致性测试
GET  /api/research/weights         因子权重优化状态
GET  /api/research/scaling         盈利加仓评估
```

### Trades (交易诊断)
```
POST /api/trades/upload            上传嘉信CSV
POST /api/trades/review            LLM诊断报告
POST /api/trades/review/stream     流式诊断
```

### System
```
GET  /api/health     健康检查
GET  /api/models     可用模型列表
```

## 技术栈

- **Web框架**: FastAPI + Uvicorn
- **数据库**: SQLAlchemy + SQLite (reports.db)
- **LLM接入**: OpenAI SDK (AsyncOpenAI)，统一适配 DeepSeek/Qwen/OpenAI/Claude
- **数据获取**: httpx (async) + yfinance + AKShare + Tavily
- **量化计算**: pandas + numpy + scipy (Newey-West HAC)
- **前端**: 纯静态 HTML + marked.js (Markdown渲染) + Chart.js

## 环境变量

```bash
DEEPSEEK_API_KEY=sk-xxx          # DeepSeek 原生 API
QWEN_API_KEY=sk-xxx              # 通义千问 DashScope
LLM_API_KEY=sk-xxx               # CloseAI 代理(Gemini/GPT/Claude)
LLM_BASE_URL=https://...         # 代理地址(可选)
LLM_MODEL=deepseek-reasoner     # 默认模型
TAVILY_API_KEY=tvly-xxx          # Tavily 搜索
RAG_API_URL=http://...           # RAG 服务地址
RAG_API_KEY=xxx                  # RAG 鉴权
HOLDER_DB_PATH=/path/trading.db  # holderAndAction 持仓库
PORT=8002                        # 服务端口
```

## 本地开发

```bash
cd InvestmentResearchWithLLM
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 填写 API Keys
source .env
uvicorn main:app --reload --port 8002
```

访问 `http://localhost:8002` 即可使用 Chat UI。

## 部署

```bash
# 增量同步
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='data/' \
  --exclude='.env' --exclude='.venv' \
  ./ root@101.201.171.174:/opt/investment-research/

# 重启
ssh root@101.201.171.174 'systemctl restart investment-research'

# 完整部署
bash deploy.sh 101.201.171.174 root
```

## 项目关系

| 项目 | 端口 | 职责 | 交互 |
|------|------|------|------|
| finance-analysis | 8000 | LLM 信号生成、回测 | 无 |
| holderAndAction | 8001 | 持仓管理、截图解析、调仓建议 | 只读其 trading.db |
| **InvestmentResearchWithLLM** | **8002** | 行业投研、量化验证、交易诊断 | — |
