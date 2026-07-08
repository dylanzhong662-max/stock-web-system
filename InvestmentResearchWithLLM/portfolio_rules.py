"""持仓组合确定性规则 — Pre-LLM 计算层

在调用 LLM 前，从已有量化数据中推导出确定性结论和压力测试，
注入 prompt 使 LLM 无需重新推导（更可靠）。
"""


def compute_deterministic_conclusions(positions: list[dict]) -> str:
    """从持仓数据中计算确定性结论，返回 Markdown 格式文本。"""
    conclusions: list[str] = []

    # 1. Alpha significance
    alpha_conclusion = _check_alpha_significance(positions)
    if alpha_conclusion:
        conclusions.append(alpha_conclusion)

    # 2. Concentration risk (tail correlation)
    corr_conclusions = _check_concentration_risk(positions)
    conclusions.extend(corr_conclusions)

    # 3. Cost efficiency
    cost_conclusions = _check_cost_efficiency(positions)
    conclusions.extend(cost_conclusions)

    if not conclusions:
        return "（量化引擎未发现需要警示的确定性结论）"

    lines = []
    for i, c in enumerate(conclusions, 1):
        lines.append(f"{i}. {c}")
    return "\n".join(lines)


def compute_stress_scenarios(positions: list[dict]) -> str:
    """计算参数化压力测试情景，返回 Markdown 表格。"""
    if not positions:
        return "（无持仓数据）"

    weights = []
    total_value = 0.0
    for p in positions:
        w = _get_position_weight(p)
        weights.append(w)
        total_value += w

    if total_value == 0:
        return "（无法计算持仓市值）"

    # Scenario 1: Market -10%
    loss_10 = _calc_market_stress(positions, weights, total_value, -0.10)

    # Scenario 2: Market -20% with tail amplification
    loss_20 = _calc_market_stress_amplified(positions, weights, total_value, -0.20)

    # Scenario 3: Rates +100bps (hit growth stocks)
    loss_rates = _calc_rate_stress(positions, weights, total_value)

    # Find most impacted position per scenario
    worst_10 = _find_worst_position(positions, weights, total_value, -0.10)
    worst_20 = _find_worst_position(positions, weights, total_value, -0.20)
    worst_rates = _find_worst_rate_position(positions)

    lines = [
        "| 情景 | 组合预期损失 | 最大受损持仓 | 说明 |",
        "|------|------------|------------|------|",
        f"| 市场跌10% | {loss_10:+.1%} | {worst_10} | Beta 加权线性估算 |",
        f"| 市场跌20%（含尾部放大） | {loss_20:+.1%} | {worst_20} | "
        f"尾部相关性>全样本+0.15 的配对按 1.3x 放大 |",
        f"| 利率+100bps | {loss_rates:+.1%} | {worst_rates} | "
        f"HML<-0.2（成长股）额外承压 -5% |",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_position_weight(pos: dict) -> float:
    fin = pos.get("financial", {}) or {}
    price = fin.get("current_price") or pos.get("entry_price")
    qty = pos.get("quantity", 1)
    if price is None:
        return 0.0
    return float(price) * float(qty)


def _get_position_beta(pos: dict) -> float:
    beta_data = pos.get("beta_data")
    if beta_data and beta_data.get("beta") is not None:
        return float(beta_data["beta"])
    return 1.0


def _check_alpha_significance(positions: list[dict]) -> str | None:
    """如果所有持仓 alpha t-stat < 2.0，说明组合无真实 alpha。"""
    positions_with_factors = [
        p for p in positions
        if p.get("factor_data") and p["factor_data"].get("alpha_t_stat") is not None
    ]
    if len(positions_with_factors) < 2:
        return None

    all_insignificant = all(
        abs(p["factor_data"]["alpha_t_stat"]) < 2.0
        for p in positions_with_factors
    )
    if all_insignificant:
        t_stats = [f"{p['ticker']}(t={p['factor_data']['alpha_t_stat']:+.1f})"
                   for p in positions_with_factors]
        return (
            f"⚠️ **组合无真实 alpha**：所有持仓 alpha t-stat 均不显著 "
            f"({', '.join(t_stats)})，收益本质为因子贝塔暴露而非选股能力。"
            f"调仓建议应聚焦因子 timing 而非个股判断。"
        )
    return None


def _check_concentration_risk(positions: list[dict]) -> list[str]:
    """检测尾部相关性 >= 0.7 的配对。"""
    conclusions = []
    corr_data = None
    for p in positions:
        if p.get("_corr_data"):
            corr_data = p["_corr_data"]
            break

    if not corr_data:
        return conclusions

    tail_pairs = corr_data.get("tail_pairs", {})
    full_pairs = corr_data.get("pairs", {})

    for pair_key, tail_corr in tail_pairs.items():
        if tail_corr >= 0.7:
            full_corr = full_pairs.get(pair_key, 0)
            amplification = tail_corr - full_corr
            conclusions.append(
                f"⚠️ **尾部集中风险** {pair_key}：危机时相关性 {tail_corr:.2f}"
                f"（全样本 {full_corr:.2f}，放大 +{amplification:.2f}）。"
                f"市场系统性下跌时这两个持仓将同步暴跌，分散化失效。"
            )

    return conclusions


def _check_cost_efficiency(positions: list[dict]) -> list[str]:
    """检测年化拖累过高的持仓。"""
    conclusions = []
    for p in positions:
        fin = p.get("financial", {}) or {}
        pnl_pct = p.get("pnl_pct")
        if pnl_pct is None:
            continue

        annualized_pnl_proxy = abs(pnl_pct) * 4 if abs(pnl_pct) > 0 else 0.05

        from transaction_costs import estimate_cost
        price = fin.get("current_price") or p.get("entry_price")
        qty = p.get("quantity", 1)
        if not price:
            continue
        trade_value = float(price) * float(qty) * 0.2
        cost = estimate_cost(p["ticker"], trade_value)

        if cost.annualized_drag_pct > 0 and annualized_pnl_proxy > 0:
            cost_alpha_ratio = (cost.annualized_drag_pct / 100) / annualized_pnl_proxy
            if cost_alpha_ratio > 0.33:
                conclusions.append(
                    f"💰 **成本效率低** {p['ticker']}：年化交易拖累 "
                    f"{cost.annualized_drag_pct:.1f}bps，"
                    f"占预期收益 {cost_alpha_ratio:.0%}（>33%阈值）。"
                    f"建议减少调仓频率或增大持仓周期。"
                )

    return conclusions


def _calc_market_stress(
    positions: list[dict],
    weights: list[float],
    total_value: float,
    market_drop: float,
) -> float:
    """Beta 加权的市场下跌估算。"""
    loss = 0.0
    for pos, w in zip(positions, weights):
        if w == 0 or total_value == 0:
            continue
        beta = _get_position_beta(pos)
        position_loss = beta * market_drop * (w / total_value)
        loss += position_loss
    return loss


def _calc_market_stress_amplified(
    positions: list[dict],
    weights: list[float],
    total_value: float,
    market_drop: float,
) -> float:
    """市场下跌 + 尾部相关性放大。"""
    base_loss = _calc_market_stress(positions, weights, total_value, market_drop)

    corr_data = None
    for p in positions:
        if p.get("_corr_data"):
            corr_data = p["_corr_data"]
            break

    if not corr_data:
        return base_loss

    tail_pairs = corr_data.get("tail_pairs", {})
    full_pairs = corr_data.get("pairs", {})

    amplification_count = 0
    for pair_key, tail_corr in tail_pairs.items():
        full_corr = full_pairs.get(pair_key, 0)
        if tail_corr - full_corr >= 0.15:
            amplification_count += 1

    if amplification_count > 0:
        n_pairs = max(1, len([p for p in positions if _get_position_weight(p) > 0]))
        total_pairs = n_pairs * (n_pairs - 1) / 2 if n_pairs > 1 else 1
        amp_ratio = amplification_count / total_pairs
        amplifier = 1.0 + 0.3 * amp_ratio
        return base_loss * amplifier

    return base_loss


def _calc_rate_stress(
    positions: list[dict],
    weights: list[float],
    total_value: float,
) -> float:
    """利率 +100bps 对成长股的额外冲击。"""
    loss = 0.0
    for pos, w in zip(positions, weights):
        if w == 0 or total_value == 0:
            continue
        factor_data = pos.get("factor_data")
        hml = 0.0
        if factor_data and factor_data.get("hml"):
            hml = factor_data["hml"].get("beta", 0.0)

        weight_frac = w / total_value
        base_hit = -0.02 * weight_frac

        if hml < -0.2:
            extra_hit = -0.05 * weight_frac
            loss += base_hit + extra_hit
        else:
            loss += base_hit

    return loss


def _find_worst_position(
    positions: list[dict],
    weights: list[float],
    total_value: float,
    market_drop: float,
) -> str:
    """找到在市场下跌中受损最大的持仓。"""
    worst_ticker = "N/A"
    worst_loss = 0.0
    for pos, w in zip(positions, weights):
        if w == 0:
            continue
        beta = _get_position_beta(pos)
        position_loss = beta * market_drop
        if position_loss < worst_loss:
            worst_loss = position_loss
            worst_ticker = f"{pos['ticker']}({position_loss:+.1%})"
    return worst_ticker


def _find_worst_rate_position(positions: list[dict]) -> str:
    """找到对利率上升最敏感的持仓。"""
    worst_ticker = "N/A"
    worst_hml = 0.0
    for pos in positions:
        factor_data = pos.get("factor_data")
        if not factor_data or not factor_data.get("hml"):
            continue
        hml = factor_data["hml"].get("beta", 0.0)
        if hml < worst_hml:
            worst_hml = hml
            worst_ticker = f"{pos['ticker']}(HML={hml:+.2f})"
    return worst_ticker
