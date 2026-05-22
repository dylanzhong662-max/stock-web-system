# Graph Report - InvestmentResearchWithLLM  (2026-05-21)

## Corpus Check
- 37 files · ~53,792 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 430 nodes · 607 edges · 26 communities (24 shown, 2 thin omitted)
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 47 edges (avg confidence: 0.67)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `155bab60`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 25|Community 25]]

## God Nodes (most connected - your core abstractions)
1. `PortfolioResearch` - 21 edges
2. `InvestmentResearchWithLLM — CLAUDE.md` - 14 edges
3. `CompanyAnalyzer` - 13 edges
4. `行业投研助手 — 设计文档` - 13 edges
5. `ChainAnalyzer` - 11 edges
6. `get_client()` - 10 edges
7. `fmp_ticker()` - 10 edges
8. `_fmp_ticker()` - 10 edges
9. `resolve_model()` - 9 edges
10. `Orchestrator` - 9 edges

## Surprising Connections (you probably didn't know these)
- `save_cache()` --calls--> `ReportCache`  [INFERRED]
  report_generator.py → models.py
- `_persist()` --calls--> `Prediction`  [INFERRED]
  predictions.py → models.py
- `extract_via_llm()` --calls--> `get_client()`  [INFERRED]
  predictions.py → llm_client.py
- `extract_from_report()` --calls--> `get_client()`  [INFERRED]
  watchlist.py → llm_client.py
- `list_models()` --calls--> `resolve_model()`  [INFERRED]
  main.py → llm_client.py

## Communities (26 total, 2 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (57): audit_price_data(), _av_ticker(), _fallback_overview_beta(), _fin_cache_get(), _fin_cache_set(), _fmp_ticker(), _get_akshare_daily(), get_atr_stops() (+49 more)

### Community 1 - "Community 1"
Cohesion: 0.08
Nodes (24): BaseModel, _annotate_news_staleness(), ChainAnalyzer, _extract_tickers(), _fmt_financial(), P0: dual Tavily + RAG; P2: yfinance for extracted tickers., Adds a staleness warning to news items older than _STALE_NEWS_DAYS., Scan search results for US ticker-like symbols (e.g. $NVDA or standalone NVDA). (+16 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (34): 5.1 `orchestrator.py`, 5.2 `chain_analyzer.py`, 5.3 `company_analyzer.py`, 5.4 `portfolio_research.py`, 5.5 `data_fetcher.py`, 5.6 `report_generator.py`, Chat（A 方向）, code:block1 (用户输入（Chat UI / REST API）) (+26 more)

### Community 3 - "Community 3"
Cohesion: 0.10
Nodes (19): API 端点速查, code:block1 (InvestmentResearchWithLLM/), code:block2 (DEEPSEEK_API_KEY=sk-xxx), code:block3 (GET  /api/health), code:bash (# 手动添加), code:bash (# 本地增量同步代码到服务器), InvestmentResearchWithLLM — CLAUDE.md, 与现有系统的关系 (+11 more)

### Community 4 - "Community 4"
Cohesion: 0.11
Nodes (8): check_watchlist_alerts(), predictions_analytics(), predictions_calibration(), 手动触发结算。force=true 会用当前价格提前结算（测试用）。, 完整分析：confidence 校准 + walk-forward + IC decay, Confidence 校准分析：模型置信度 vs 实际命中率, 检查所有监控项阈值，触发时推飞书。可 cron 定时调用。, resolve_predictions()

### Community 5 - "Community 5"
Cohesion: 0.16
Nodes (14): Base, Prediction, LLM 报告中的方向性判断，用于后续计算命中率 / IC, 监控清单项——从持仓分析报告中提取的关键变量, WatchItem, add_item(), batch_add(), build_watchlist_context() (+6 more)

### Community 6 - "Community 6"
Cohesion: 0.17
Nodes (14): extract(), extract_via_llm(), _fetch_price(), _normalize(), _persist(), 预测落库 + 结算 + 命中率统计  预测来源： 1. 主报告末尾的 ```prediction 代码块（容易被 max_tokens 截断） 2. 兜底：报告, 独立二次调用抽取预测，规避主报告截断问题。返回落库条数。, 拉收盘价。优先用 FMP/AKShare（服务器上 yfinance 不稳定）。      as_of 接近当前时间时直接用实时价格；历史价格走 AV 日线缓存 (+6 more)

### Community 7 - "Community 7"
Cohesion: 0.25
Nodes (12): bk_header(), count_xfs(), get_row_data(), LogHandler, main(), print_labels(), show(), show_fonts() (+4 more)

### Community 8 - "Community 8"
Cohesion: 0.22
Nodes (13): _build_feishu_message(), check_alerts(), _check_direction(), _fetch_current_value(), _is_triggered(), _parse_threshold(), 监控清单预警 → 飞书推送  定时检查 watchlist 中每个活跃项的当前值，对比 bullish/bearish 阈值， 触发时发送飞书 webhook, 从阈值描述中提取数字。支持 '>4.5%' / '<20' / '突破 150' 等格式 (+5 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (13): 1. 产业链分析, 2. 公司分析, 3. 持仓研究报告, code:block1 (分析 AI 算力产业链), code:block2 (分析英伟达), code:block3 (分析我的持仓), code:block4 (自上而下：                          自下而上（深度补充）：), 三种核心用法 (+5 more)

### Community 10 - "Community 10"
Cohesion: 0.15
Nodes (12): code:`, code:block2, 产业链分层、玩家、利润池与竞争强度, 产业链分析框架 Prompt, 代表性公司财务数据（来自 yfinance / FMP，为 ground truth，优先用于财务数字引用）, 参考信息, 哪些层最赚钱、哪些层最容易被卷, 【强制附录：结构化预测】 (+4 more)

### Community 11 - "Community 11"
Cohesion: 0.06
Nodes (53): fin_cache_get(), fin_cache_set(), price_cache_get(), price_cache_set(), fallback_overview_beta(), _get_av_overview(), get_batch_stock_data(), _get_fmp_price() (+45 more)

### Community 12 - "Community 12"
Cohesion: 0.17
Nodes (11): code:`, code:block2, 一、组合概览, 三、逐仓深度分析, 二、风险量化分析, 五、关键变量监控清单（下一步看哪些数据）, 六、产业链集中度风险, 四、组合建议（按优先级排序） (+3 more)

### Community 13 - "Community 13"
Cohesion: 0.27
Nodes (10): confidence_calibration(), full_analytics(), ic_decay_analysis(), _interpret_calibration(), _interpret_decay(), 预测分析：confidence calibration + walk-forward + IC decay  补齐评分中指出的关键缺陷： 1. Confiden, 滚动窗口 walk-forward 分析：按月统计命中率、IC、Sharpe 的稳定性      检验策略是否时间一致（非偶然某段好）, IC Decay：预测发出后不同时间点的方向正确率      评估信号衰减速度——30 天 horizon 的预测，可能在第 7 天就已经 price-in 了 (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.25
Nodes (9): _asset_class(), CostEstimate, estimate_cost(), estimate_portfolio_costs(), format_cost_section(), 交易成本模型  估算每笔建议的往返成本（round-trip）：   1. 佣金（commission）   2. 买卖价差（bid-ask spread）, 估算组合级别的年化交易成本      Args:         positions: enriched 持仓列表（含 financial.current_pr, 格式化为 Markdown 段落，注入持仓分析 prompt (+1 more)

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (5): PortfolioResearch, 市值加权 Beta（quantity × current_price），R²<0.1 的持仓降权 50%, 市值加权的组合 Fama-French 因子暴露, RAG 优先，RAG 空时用 Tavily 兜底，两者均空时返回空字符串, _read_positions()

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (10): 1. 产业链定位, 2. 财务快照, 3. 核心投资逻辑（3条 bullets，每条必须是判断而非描述）, 4. 关键变量（买这只股票需要跟踪哪些数据）, 5. 可能被低估的点（逆向视角）, 6. 主要风险（3条，每条说明对估值的潜在影响）, 7. 结论, {company}（{ticker}）投研快照 (+2 more)

### Community 17 - "Community 17"
Cohesion: 0.25
Nodes (7): fmt_news_context(), get_signals(), news-rag-system 接入客户端  环境变量：   RAG_API_URL  — RAG 服务地址，默认 http://43.139.5.125:80, 语义检索 RAG 新闻，返回 chunk 列表，失败时返回 []（静默降级）      data_types 可选: news / sec_filing / r, 查询 RAG 系统中的结构化信号，失败时返回 [], 把 RAG 结果格式化为 prompt 注入文本, search_news()

### Community 18 - "Community 18"
Cohesion: 0.40
Nodes (3): format_report(), 规范化 LLM 输出的 Markdown，添加时间戳和数据来源, save_cache()

### Community 20 - "Community 20"
Cohesion: 0.67
Nodes (3): main(), 预热价格缓存：把所有持仓 ticker + FF 因子 ETF 的日线数据拉到本地 SQLite  用法：   .venv/bin/python warmup_, _read_portfolio_tickers()

## Knowledge Gaps
- **64 isolated node(s):** `allow`, `一、项目定位`, `二、核心分析框架（来自产品图）`, `code:block1 (用户输入（Chat UI / REST API）)`, `意图类型` (+59 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_client()` connect `Community 1` to `Community 5`, `Community 6`, `Community 15`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Why does `PortfolioResearch` connect `Community 15` to `Community 1`?**
  _High betweenness centrality (0.018) - this node is a cross-community bridge._
- **Why does `extract_via_llm()` connect `Community 6` to `Community 1`?**
  _High betweenness centrality (0.015) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `PortfolioResearch` (e.g. with `Orchestrator` and `CompanyAnalyzer`) actually correct?**
  _`PortfolioResearch` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `CompanyAnalyzer` (e.g. with `Orchestrator` and `PortfolioResearch`) actually correct?**
  _`CompanyAnalyzer` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `ChainAnalyzer` (e.g. with `Orchestrator` and `ChainRequest`) actually correct?**
  _`ChainAnalyzer` has 6 INFERRED edges - model-reasoned connections that need verification._
- **What connects `预测分析：confidence calibration + walk-forward + IC decay  补齐评分中指出的关键缺陷： 1. Confiden`, `按 confidence 分桶统计实际命中率，检验模型是否校准      完美校准：confidence=0.6 的预测，实际命中率也应 ≈ 60%     返`, `滚动窗口 walk-forward 分析：按月统计命中率、IC、Sharpe 的稳定性      检验策略是否时间一致（非偶然某段好）` to the rest of the system?**
  _155 weakly-connected nodes found - possible documentation gaps or missing edges._