# Graph Report - InvestmentResearchWithLLM  (2026-06-26)

## Corpus Check
- 53 files · ~74,645 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1030 nodes · 2032 edges · 52 communities (51 shown, 1 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 121 edges (avg confidence: 0.56)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6af80a06`
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
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]

## God Nodes (most connected - your core abstractions)
1. `get_client()` - 31 edges
2. `PortfolioResearch` - 31 edges
3. `ChainAnalyzer` - 26 edges
4. `CompanyAnalyzer` - 26 edges
5. `TechnicalAnalyzer` - 26 edges
6. `resolve_model()` - 25 edges
7. `build_extra_params()` - 24 edges
8. `fmp_ticker()` - 22 edges
9. `has_reasoning()` - 21 edges
10. `PortfolioResearch` - 21 edges

## Surprising Connections (you probably didn't know these)
- `float` --uses--> `ChainAnalyzer`  [INFERRED]
  consistency_test.py → chain_analyzer.py
- `float` --uses--> `Prediction`  [INFERRED]
  neglect_weight_optimizer.py → models.py
- `int` --uses--> `Prediction`  [INFERRED]
  neglect_weight_optimizer.py → models.py
- `save_cache()` --calls--> `ReportCache`  [INFERRED]
  report_generator.py → models.py
- `int` --uses--> `ChainAnalyzer`  [INFERRED]
  consistency_test.py → chain_analyzer.py

## Import Cycles
- 1-file cycle: `backtest_simulation.py -> backtest_simulation.py`
- 2-file cycle: `backtest_simulation.py -> data_providers/intl_screener.py -> backtest_simulation.py`
- 3-file cycle: `backtest_simulation.py -> data_providers/intl_screener.py -> data_providers/cache.py -> backtest_simulation.py`
- 3-file cycle: `backtest_simulation.py -> data_providers/price_series.py -> data_providers/cache.py -> backtest_simulation.py`

## Communities (52 total, 1 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (57): audit_price_data(), _av_ticker(), _fallback_overview_beta(), _fin_cache_get(), _fin_cache_set(), _fmp_ticker(), _get_akshare_daily(), get_atr_stops() (+49 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (19): _annotate_news_staleness(), _extract_tickers(), _fmt_financial(), P0: dual Tavily + RAG; P2: yfinance for extracted tickers., Adds a staleness warning to news items older than _STALE_NEWS_DAYS., Scan search results for US ticker-like symbols (e.g. $NVDA or standalone NVDA)., One-line financial summary for prompt injection., get_client() (+11 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (34): 5.1 `orchestrator.py`, 5.2 `chain_analyzer.py`, 5.3 `company_analyzer.py`, 5.4 `portfolio_research.py`, 5.5 `data_fetcher.py`, 5.6 `report_generator.py`, Chat（A 方向）, code:block1 (用户输入（Chat UI / REST API）) (+26 more)

### Community 3 - "Community 3"
Cohesion: 0.10
Nodes (19): API 端点速查, code:block1 (InvestmentResearchWithLLM/), code:block2 (DEEPSEEK_API_KEY=sk-xxx), code:block3 (GET  /api/health), code:bash (# 手动添加), code:bash (# 本地增量同步代码到服务器), InvestmentResearchWithLLM — CLAUDE.md, 与现有系统的关系 (+11 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (69): Base, BaseModel, ChainAnalyzer, CompanyAnalyzer, ChainAnalyzer, CompanyAnalyzer, _fmt(), Prediction (+61 more)

### Community 5 - "Community 5"
Cohesion: 0.26
Nodes (9): add_item(), batch_add(), build_watchlist_context(), extract_and_save(), extract_from_report(), list_active(), 监控清单系统  功能： 1. 从持仓分析报告中自动提取"关键变量监控清单"（第五章节） 2. 手动添加/删除/更新监控项 3. 在下次分析时，将现有监控项注入, 构建注入到持仓分析 prompt 的监控上下文 (+1 more)

### Community 6 - "Community 6"
Cohesion: 0.17
Nodes (14): extract(), extract_via_llm(), _fetch_price(), _normalize(), _persist(), 预测落库 + 结算 + 命中率统计  预测来源： 1. 主报告末尾的 ```prediction 代码块（容易被 max_tokens 截断） 2. 兜底：报告, 独立二次调用抽取预测，规避主报告截断问题。返回落库条数。, 拉收盘价。优先用 FMP/AKShare（服务器上 yfinance 不稳定）。      as_of 接近当前时间时直接用实时价格；历史价格走 AV 日线缓存 (+6 more)

### Community 7 - "Community 7"
Cohesion: 0.30
Nodes (12): bk_header(), count_xfs(), get_row_data(), LogHandler, main(), print_labels(), show(), show_fonts() (+4 more)

### Community 8 - "Community 8"
Cohesion: 0.22
Nodes (13): _build_feishu_message(), check_alerts(), _check_direction(), _fetch_current_value(), _is_triggered(), _parse_threshold(), 监控清单预警 → 飞书推送  定时检查 watchlist 中每个活跃项的当前值，对比 bullish/bearish 阈值， 触发时发送飞书 webhook, 从阈值描述中提取数字。支持 '>4.5%' / '<20' / '突破 150' 等格式 (+5 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (13): 1. 产业链分析, 2. 公司分析, 3. 持仓研究报告, code:block1 (分析 AI 算力产业链), code:block2 (分析英伟达), code:block3 (分析我的持仓), code:block4 (自上而下：                          自下而上（深度补充）：), 三种核心用法 (+5 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (14): code:`, code:block2, 产业链分层、玩家、利润池与竞争强度, 产业链分析框架 Prompt, 代表性公司财务数据（来自 yfinance / FMP，为 ground truth，优先用于财务数字引用）, 参考信息, 哪些层最赚钱、哪些层最容易被卷, 国际 Neglect-Alpha 候选标的（量化筛选结果，用于第9问B部分） (+6 more)

### Community 11 - "Community 11"
Cohesion: 0.06
Nodes (82): AsyncClient, 兼容层 — 原 data_fetcher.py 已拆分到 data_providers/ 包  所有外部模块仍可通过 `import data_fetcher`, fin_cache_get(), fin_cache_set(), _init(), price_cache_get(), price_cache_set(), _build_snapshot() (+74 more)

### Community 12 - "Community 12"
Cohesion: 0.12
Nodes (15): code:`, code:block2, 一、组合概览, 七、产业链集中度风险, 三、逐仓深度分析, 二、风险量化分析, 五、关键变量监控清单（下一步看哪些数据）, 五、组合建议（按优先级排序） (+7 more)

### Community 13 - "Community 13"
Cohesion: 0.27
Nodes (10): confidence_calibration(), full_analytics(), ic_decay_analysis(), _interpret_calibration(), _interpret_decay(), 预测分析：confidence calibration + walk-forward + IC decay  补齐评分中指出的关键缺陷： 1. Confiden, 滚动窗口 walk-forward 分析：按月统计命中率、IC、Sharpe 的稳定性      检验策略是否时间一致（非偶然某段好）, IC Decay：预测发出后不同时间点的方向正确率      评估信号衰减速度——30 天 horizon 的预测，可能在第 7 天就已经 price-in 了 (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.25
Nodes (9): _asset_class(), CostEstimate, estimate_cost(), estimate_portfolio_costs(), format_cost_section(), 交易成本模型  估算每笔建议的往返成本（round-trip）：   1. 佣金（commission）   2. 买卖价差（bid-ask spread）, 估算组合级别的年化交易成本      Args:         positions: enriched 持仓列表（含 financial.current_pr, 格式化为 Markdown 段落，注入持仓分析 prompt (+1 more)

### Community 15 - "Community 15"
Cohesion: 0.05
Nodes (65): _compute_neglect_score(), _detect_ssl_verify(), _extract_tickers_from_text(), _fmp_intl_fallback(), _fmp_profiles(), _fmp_us_analyst_count(), _fmp_us_growth(), format_neglect_candidates() (+57 more)

### Community 16 - "Community 16"
Cohesion: 0.17
Nodes (10): 1. 产业链定位, 2. 财务快照, 3. 核心投资逻辑（3条 bullets，每条必须是判断而非描述）, 4. 关键变量（买这只股票需要跟踪哪些数据）, 5. 可能被低估的点（逆向视角）, 6. 主要风险（3条，每条说明对估值的潜在影响）, 7. 结论, {company}（{ticker}）投研快照 (+2 more)

### Community 17 - "Community 17"
Cohesion: 0.25
Nodes (7): fmt_news_context(), get_signals(), news-rag-system 接入客户端  环境变量：   RAG_API_URL  — RAG 服务地址，默认 http://43.139.5.125:80, 语义检索 RAG 新闻，返回 chunk 列表，失败时返回 []（静默降级）      data_types 可选: news / sec_filing / r, 查询 RAG 系统中的结构化信号，失败时返回 [], 把 RAG 结果格式化为 prompt 注入文本, search_news()

### Community 18 - "Community 18"
Cohesion: 0.40
Nodes (3): format_report(), 规范化 LLM 输出的 Markdown，添加时间戳和数据来源, save_cache()

### Community 19 - "Community 19"
Cohesion: 0.53
Nodes (4): Get-PyVenvConfig(), global:deactivate(), global:_OLD_VIRTUAL_PROMPT(), global:prompt()

### Community 20 - "Community 20"
Cohesion: 0.67
Nodes (3): main(), 预热价格缓存：把所有持仓 ticker + FF 因子 ETF 的日线数据拉到本地 SQLite  用法：   .venv/bin/python warmup_, _read_portfolio_tickers()

### Community 23 - "Community 23"
Cohesion: 0.11
Nodes (36): DataFrame, _read_open_positions(), Series, calc_atr(), calc_bollinger(), calc_ema(), calc_macd(), calc_rsi() (+28 more)

### Community 25 - "Community 25"
Cohesion: 0.06
Nodes (34): 5.1 `orchestrator.py`, 5.2 `chain_analyzer.py`, 5.3 `company_analyzer.py`, 5.4 `portfolio_research.py`, 5.5 `data_fetcher.py`, 5.6 `report_generator.py`, Chat（A 方向）, code:block1 (用户输入（Chat UI / REST API）) (+26 more)

### Community 26 - "Community 26"
Cohesion: 0.13
Nodes (28): _calc_market_stress(), _calc_market_stress_amplified(), _calc_rate_stress(), _check_alpha_significance(), _check_concentration_risk(), _check_cost_efficiency(), compute_deterministic_conclusions(), compute_stress_scenarios() (+20 more)

### Community 27 - "Community 27"
Cohesion: 0.08
Nodes (24): 1. 产业链分析 (ChainAnalyzer), 2. 公司分析 (CompanyAnalyzer), 3. 持仓研究 (PortfolioResearch), 4. 交易行为诊断 (TradeAnalytics), 5. 盈利加仓顾问 (ScalingAdvisor), 6. 量化验证体系, 7. 监控清单系统 (Watchlist), API 端点 (+16 more)

### Community 28 - "Community 28"
Cohesion: 0.30
Nodes (13): AsyncOpenAI, build_extra_params(), get_client(), has_reasoning(), is_deepseek(), is_glm(), is_qwen(), bool (+5 more)

### Community 29 - "Community 29"
Cohesion: 0.16
Nodes (18): date, _build_scale_plan(), _compute_atr(), _compute_ma5(), _days_since_entry(), _detect_upper_shadow_with_volume(), _determine_scale_tier(), evaluate_scaling() (+10 more)

### Community 30 - "Community 30"
Cohesion: 0.17
Nodes (14): extract(), extract_via_llm(), _fetch_price(), _normalize(), _persist(), 预测落库 + 结算 + 命中率统计  预测来源： 1. 主报告末尾的 ```prediction 代码块（容易被 max_tokens 截断） 2. 兜底：报告, 独立二次调用抽取预测，规避主报告截断问题。返回落库条数。, 拉收盘价。优先用 FMP/AKShare（服务器上 yfinance 不稳定）。      as_of 接近当前时间时直接用实时价格；历史价格走 AV 日线缓存 (+6 more)

### Community 31 - "Community 31"
Cohesion: 0.12
Nodes (15): API 端点速查, InvestmentResearchWithLLM — CLAUDE.md, 与现有系统的关系, 常见问题, 意图路由逻辑, 数据来源与可信度, 模型分工, 环境变量 (+7 more)

### Community 32 - "Community 32"
Cohesion: 0.19
Nodes (11): _fmt(), str, resolve_model(), str, 交易分析 API — 上传嘉信 CSV → 解析 → 分析 → LLM 诊断, 上传嘉信 CSV 文件，返回解析结果 + 量化分析, 基于最近一次上传的分析结果，生成 LLM 诊断报告, trade_review() (+3 more)

### Community 33 - "Community 33"
Cohesion: 0.12
Nodes (15): 2.1 Confidence 校准——完全反转, 2.2 IC Decay——60天 horizon 有强负信号, 2.3 因子权重优化结果, 2.4 一致性测试（AI算力, 3次运行）, 2.5 参数敏感性, Neglect Alpha 策略验证研究报告, 一、核心结论, 三、问题根因分析 (+7 more)

### Community 34 - "Community 34"
Cohesion: 0.25
Nodes (14): _build_review_prompt(), compute_analytics(), generate_review(), match_round_trips(), _parse_dollar(), _parse_number(), parse_schwab_csv(), float (+6 more)

### Community 35 - "Community 35"
Cohesion: 0.24
Nodes (14): _get_price_at(), _interpret_backtest(), _normal_cdf(), float, int, str, Backtest Simulation — 用历史数据验证 Neglect Alpha 筛选策略的样本外表现  方法论： 1. 回溯 N 个月，每月初用 int, Walk-forward backtest: re-screen every month, run forward for horizon_days. (+6 more)

### Community 36 - "Community 36"
Cohesion: 0.16
Nodes (15): check_trade(), fmt_news_context(), fmt_risk_overlay(), get_risk_overlay(), get_signals(), float, int, str (+7 more)

### Community 37 - "Community 37"
Cohesion: 0.22
Nodes (11): 监控清单项——从持仓分析报告中提取的关键变量, WatchItem, add_item(), batch_add(), build_watchlist_context(), extract_and_save(), extract_from_report(), list_active() (+3 more)

### Community 38 - "Community 38"
Cohesion: 0.22
Nodes (13): _build_feishu_message(), check_alerts(), _check_direction(), _fetch_current_value(), _is_triggered(), _parse_threshold(), 监控清单预警 → 飞书推送  定时检查 watchlist 中每个活跃项的当前值，对比 bullish/bearish 阈值， 触发时发送飞书 webhook, 从阈值描述中提取数字。支持 '>4.5%' / '<20' / '突破 150' 等格式 (+5 more)

### Community 39 - "Community 39"
Cohesion: 0.14
Nodes (13): 1. 产业链分析, 2. 公司分析, 3. 持仓研究报告, code:block1 (分析 AI 算力产业链), code:block2 (分析英伟达), code:block3 (分析我的持仓), code:block4 (自上而下：                          自下而上（深度补充）：), 三种核心用法 (+5 more)

### Community 40 - "Community 40"
Cohesion: 0.23
Nodes (9): _annotate_news_staleness(), _extract_tickers(), _fmt_financial(), bool, str, P0: dual Tavily + RAG; P2: yfinance for extracted tickers; P3: intl neglect scre, Adds a staleness warning to news items older than _STALE_NEWS_DAYS., Scan search results for US ticker-like symbols (e.g. $NVDA or standalone NVDA). (+1 more)

### Community 41 - "Community 41"
Cohesion: 0.24
Nodes (12): _compress_batch(), filter_neglect_by_sector(), _is_adjacent_sector(), bool, str, 数据预处理 — 搜索结果压缩 + 行业筛选  在调用重推理模型前： 1. 用轻量模型(deepseek-v4-pro)批量压缩搜索结果，降低注意力稀释 2. 用, 从 industry_seed_lists._SECTOR_MAP 解析目标 sector。, 允许相邻行业通过（如 Technology 目标允许 Industrials 中的设备商）。 (+4 more)

### Community 42 - "Community 42"
Cohesion: 0.27
Nodes (12): _build_truth_map(), _compare_claim(), _empty_result(), _extract_claims_regex(), format_credibility_section(), str, 报告落地验证 — 提取 LLM 输出中的数字声明，与注入的 ground truth 对比  偏差 > 20% 的声明标记为幻觉，输出"数据可信度"附录。, 将单条声明与 ground truth 对比。 (+4 more)

### Community 43 - "Community 43"
Cohesion: 0.20
Nodes (5): migrate(), Drop old unique index on cache_key (now uses composite report_type+cache_key)., list_models(), Prediction, LLM 报告中的方向性判断，用于后续计算命中率 / IC

### Community 44 - "Community 44"
Cohesion: 0.27
Nodes (10): confidence_calibration(), full_analytics(), ic_decay_analysis(), _interpret_calibration(), _interpret_decay(), 预测分析：confidence calibration + walk-forward + IC decay  补齐评分中指出的关键缺陷： 1. Confiden, 滚动窗口 walk-forward 分析：按月统计命中率、IC、Sharpe 的稳定性      检验策略是否时间一致（非偶然某段好）, IC Decay：预测发出后不同时间点的方向正确率      评估信号衰减速度——30 天 horizon 的预测，可能在第 7 天就已经 price-in 了 (+2 more)

### Community 45 - "Community 45"
Cohesion: 0.33
Nodes (9): _interpret_consistency(), float, int, str, Consistency Test — 同一产业链跑 N 次分析，比较预测一致性  核心假设：如果 LLM 对同一输入给出不同方向判断，信号为零。 一个真正的 a, Run N analyses of the same industry and compare prediction consistency.      Thi, Run one chain analysis and extract predictions., run_consistency_test() (+1 more)

### Community 46 - "Community 46"
Cohesion: 0.20
Nodes (9): {ticker} 技术分析报告, 一、趋势判定（多时间框架）, 三、动量与超买超卖, 二、关键价位（支撑/阻力区间）, 五、交易计划, 六、风险提示, 四、形态与信号, 【强制附录：结构化预测】 (+1 more)

### Community 47 - "Community 47"
Cohesion: 0.39
Nodes (4): Orchestrator, str, chat_stream(), ChatRequest

### Community 48 - "Community 48"
Cohesion: 0.43
Nodes (7): _composite_key(), format_report(), get_cached(), str, 同一主题 + 同一模型才命中缓存，不同模型重新生成, 规范化 LLM 输出的 Markdown，添加时间戳和数据来源, save_cache()

### Community 49 - "Community 49"
Cohesion: 0.29
Nodes (6): 一、总览, 三、操作优先级（技术面排序）, 二、逐仓分析, 四、风险提示, 持仓技术面综合分析, 持仓技术面综合报告

### Community 50 - "Community 50"
Cohesion: 0.67
Nodes (3): main(), 预热价格缓存：把所有持仓 ticker + FF 因子 ETF 的日线数据拉到本地 SQLite  用法：   .venv/bin/python warmup_, _read_portfolio_tickers()

## Knowledge Gaps
- **167 isolated node(s):** `allow`, `bool`, `bool`, `float`, `float` (+162 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `datetime` connect `Community 35` to `Community 34`, `Community 37`, `Community 38`, `Community 43`, `Community 11`, `Community 45`, `Community 44`, `Community 15`, `Community 48`, `Community 28`, `Community 29`, `Community 30`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **Why does `PortfolioResearch` connect `Community 1` to `Community 4`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Why does `resolve_model()` connect `Community 32` to `Community 34`, `Community 4`, `Community 40`, `Community 43`, `Community 45`, `Community 47`, `Community 23`, `Community 28`?**
  _High betweenness centrality (0.024) - this node is a cross-community bridge._
- **Are the 14 inferred relationships involving `PortfolioResearch` (e.g. with `Orchestrator` and `str`) actually correct?**
  _`PortfolioResearch` has 14 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `ChainAnalyzer` (e.g. with `float` and `int`) actually correct?**
  _`ChainAnalyzer` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `CompanyAnalyzer` (e.g. with `Orchestrator` and `str`) actually correct?**
  _`CompanyAnalyzer` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `TechnicalAnalyzer` (e.g. with `Orchestrator` and `str`) actually correct?**
  _`TechnicalAnalyzer` has 13 INFERRED edges - model-reasoned connections that need verification._