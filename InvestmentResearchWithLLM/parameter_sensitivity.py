"""Parameter Sensitivity Analysis — 验证 Neglect Score 阈值的稳健性

核心问题：当前阈值（min_revenue_growth=12%, max_analysts=8, market_cap 5亿-300亿）
是 hardcoded 的。如果参数 ±20% 时结果剧烈变化，说明信号不 robust。

方法：
1. 对每个关键参数做 perturbation（-30%, -15%, 0%, +15%, +30%）
2. 记录每个参数设置下的候选池大小和平均 neglect_score
3. 如果结果对参数极度敏感（CV > 0.3），标记为不稳健
"""
import asyncio
import copy
from typing import Optional

from data_providers.intl_screener import (
    NEGLECT_THRESHOLDS,
    _compute_neglect_score,
    _passes_neglect_filter,
    _get_market,
)


# Parameters to test
_PERTURBATION_LEVELS = [-0.30, -0.15, 0.0, 0.15, 0.30]

_SENSITIVE_PARAMS = [
    ("min_revenue_growth", "最低营收增速", 0.12),
    ("min_market_cap_usd", "最低市值(USD)", 5e8),
    ("max_market_cap_usd", "最高市值(USD)", 3e10),
    ("max_forward_pe", "最高 Forward PE", 50),
]

_MARKET_PARAMS = [
    ("US", "max_analysts", "美股最大分析师数", 8),
    ("JP", "max_analysts", "日股最大分析师数", 14),
    ("KR", "max_analysts", "韩股最大分析师数", 12),
    ("EU", "max_analysts", "欧股最大分析师数", 10),
]


def _apply_threshold_override(param_name: str, value: float) -> dict:
    """Create a modified threshold dict with one parameter changed."""
    modified = copy.deepcopy(NEGLECT_THRESHOLDS)
    if param_name in modified:
        modified[param_name] = value
    return modified


def _filter_with_thresholds(stocks: list[dict], thresholds: dict) -> list[dict]:
    """Apply neglect filter with custom thresholds."""
    results = []
    for stock in stocks:
        s = dict(stock)  # copy to avoid mutation
        market = _get_market(s)
        market_th = thresholds.get(market, thresholds.get("default", {}))
        if isinstance(market_th, dict):
            max_a = market_th.get("max_analysts", 10)
        else:
            max_a = 10

        analysts = s.get("analyst_count", 999)
        if analysts > max_a:
            continue

        market_cap = s.get("market_cap") or 0
        currency = s.get("currency", "USD")
        if currency == "JPY":
            market_cap_usd = market_cap / 155
        elif currency == "KRW":
            market_cap_usd = market_cap / 1350
        elif currency == "GBP":
            market_cap_usd = market_cap * 1.27
        elif currency == "EUR":
            market_cap_usd = market_cap * 1.08
        elif currency == "CHF":
            market_cap_usd = market_cap * 1.12
        else:
            market_cap_usd = market_cap

        if market_cap_usd < thresholds.get("min_market_cap_usd", 5e8):
            continue
        if market_cap_usd > thresholds.get("max_market_cap_usd", 3e10):
            continue

        s["market_cap_usd"] = market_cap_usd

        rev_growth = s.get("revenue_growth")
        earn_growth = s.get("earnings_growth")
        growth = rev_growth if rev_growth is not None else earn_growth
        if growth is None or growth < thresholds.get("min_revenue_growth", 0.12):
            continue

        pe_fwd = s.get("pe_forward")
        if pe_fwd is not None and pe_fwd > thresholds.get("max_forward_pe", 50):
            continue

        s["neglect_score"] = _compute_neglect_score(s)
        results.append(s)

    return results


def run_sensitivity(
    stock_pool: list[dict],
    perturbations: Optional[list[float]] = None,
) -> dict:
    """Run parameter sensitivity analysis on a stock pool.

    Args:
        stock_pool: raw stock data (before filtering) — typically from yfinance/FMP
        perturbations: list of perturbation levels (default: ±15%, ±30%)

    Returns:
        Per-parameter sensitivity results + overall stability assessment
    """
    if not stock_pool:
        return {"error": "Empty stock pool", "results": []}

    perturbations = perturbations or _PERTURBATION_LEVELS

    results = []

    # Test shared thresholds (min_revenue_growth, market_cap, PE)
    for param_name, display_name, base_value in _SENSITIVE_PARAMS:
        param_results = []
        for pct in perturbations:
            test_value = base_value * (1 + pct)
            modified_thresholds = copy.deepcopy(NEGLECT_THRESHOLDS)
            modified_thresholds[param_name] = test_value

            filtered = _filter_with_thresholds(stock_pool, modified_thresholds)
            scores = [s.get("neglect_score", 0) for s in filtered]
            avg_score = sum(scores) / len(scores) if scores else 0

            param_results.append({
                "perturbation": f"{pct:+.0%}",
                "value": _format_value(param_name, test_value),
                "candidates": len(filtered),
                "avg_neglect_score": round(avg_score, 1),
            })

        # Stability: CV of candidate count across perturbations
        counts = [r["candidates"] for r in param_results]
        mean_count = sum(counts) / len(counts) if counts else 0
        if mean_count > 0 and len(counts) >= 3:
            std_count = (sum((c - mean_count) ** 2 for c in counts) / len(counts)) ** 0.5
            cv = std_count / mean_count
        else:
            cv = float("inf")

        results.append({
            "parameter": param_name,
            "display_name": display_name,
            "base_value": _format_value(param_name, base_value),
            "perturbation_results": param_results,
            "cv": round(cv, 3),
            "is_stable": cv < 0.3,
            "assessment": (
                "稳健" if cv < 0.15
                else "可接受" if cv < 0.3
                else "敏感——参数选择对结果影响大" if cv < 0.5
                else "极度敏感——信号不可靠"
            ),
        })

    # Test market-specific analyst thresholds
    for market, param_name, display_name, base_value in _MARKET_PARAMS:
        param_results = []
        for pct in perturbations:
            test_value = max(1, int(base_value * (1 + pct)))
            modified_thresholds = copy.deepcopy(NEGLECT_THRESHOLDS)
            if market in modified_thresholds and isinstance(modified_thresholds[market], dict):
                modified_thresholds[market][param_name] = test_value

            filtered = _filter_with_thresholds(stock_pool, modified_thresholds)
            scores = [s.get("neglect_score", 0) for s in filtered]
            avg_score = sum(scores) / len(scores) if scores else 0

            param_results.append({
                "perturbation": f"{pct:+.0%}",
                "value": test_value,
                "candidates": len(filtered),
                "avg_neglect_score": round(avg_score, 1),
            })

        counts = [r["candidates"] for r in param_results]
        mean_count = sum(counts) / len(counts) if counts else 0
        if mean_count > 0 and len(counts) >= 3:
            std_count = (sum((c - mean_count) ** 2 for c in counts) / len(counts)) ** 0.5
            cv = std_count / mean_count
        else:
            cv = float("inf")

        results.append({
            "parameter": f"{market}.{param_name}",
            "display_name": display_name,
            "base_value": base_value,
            "perturbation_results": param_results,
            "cv": round(cv, 3),
            "is_stable": cv < 0.3,
            "assessment": (
                "稳健" if cv < 0.15
                else "可接受" if cv < 0.3
                else "敏感——参数选择对结果影响大" if cv < 0.5
                else "极度敏感——信号不可靠"
            ),
        })

    # Overall assessment
    unstable = [r for r in results if not r["is_stable"]]
    overall_cv = sum(r["cv"] for r in results) / len(results) if results else 0

    return {
        "stock_pool_size": len(stock_pool),
        "base_filter_count": len(_filter_with_thresholds(stock_pool, NEGLECT_THRESHOLDS)),
        "parameters_tested": len(results),
        "unstable_parameters": [r["display_name"] for r in unstable],
        "overall_cv": round(overall_cv, 3),
        "overall_assessment": (
            "策略对参数选择稳健" if not unstable
            else f"有 {len(unstable)} 个参数敏感，需要更多数据验证或收紧阈值"
        ),
        "results": results,
        "recommendation": _generate_recommendation(results),
    }


def _format_value(param_name: str, value: float) -> str:
    if "market_cap" in param_name:
        if value >= 1e9:
            return f"${value/1e9:.1f}B"
        return f"${value/1e6:.0f}M"
    if "growth" in param_name:
        return f"{value*100:.1f}%"
    if "pe" in param_name.lower():
        return f"{value:.0f}"
    return str(value)


def _generate_recommendation(results: list[dict]) -> list[str]:
    """Generate actionable recommendations from sensitivity analysis."""
    recs = []
    for r in results:
        if r["cv"] >= 0.5:
            recs.append(
                f"⚠️ {r['display_name']} 极度敏感 (CV={r['cv']:.2f})："
                f"考虑用数据驱动的方式确定最优阈值，而非 hardcode"
            )
        elif r["cv"] >= 0.3:
            recs.append(
                f"⚡ {r['display_name']} 中等敏感 (CV={r['cv']:.2f})："
                f"当前值 {r['base_value']} 可接受但需持续监控"
            )

    if not recs:
        recs.append("✅ 所有参数稳健——当前阈值设置合理")

    return recs
