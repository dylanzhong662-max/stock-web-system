"""报告落地验证 — 提取 LLM 输出中的数字声明，与注入的 ground truth 对比

偏差 > 20% 的声明标记为幻觉，输出"数据可信度"附录。
"""
import re
from typing import Optional

_DEVIATION_THRESHOLD = 0.20

_METRIC_PATTERNS: dict[str, list[str]] = {
    "gross_margin": [
        r"毛利率[为是约]?\s*(\d+\.?\d*)%",
        r"gross\s*margin[:\s]*(\d+\.?\d*)%",
    ],
    "operating_margin": [
        r"营业利润率[为是约]?\s*(\d+\.?\d*)%",
        r"operating\s*margin[:\s]*(\d+\.?\d*)%",
    ],
    "revenue_growth": [
        r"营收增速[为是约]?\s*(\d+\.?\d*)%",
        r"营收同比[增长涨]?\s*(\d+\.?\d*)%",
        r"revenue\s*growth[:\s]*(\d+\.?\d*)%",
    ],
    "pe_forward": [
        r"Forward\s*PE[:\s为是约]*(\d+\.?\d*)",
        r"前瞻PE[:\s为是约]*(\d+\.?\d*)",
        r"远期PE[:\s为是约]*(\d+\.?\d*)",
    ],
    "pe_ttm": [
        r"PE\s*\(TTM\)[:\s为是约]*(\d+\.?\d*)",
        r"TTM\s*PE[:\s为是约]*(\d+\.?\d*)",
    ],
    "market_cap": [
        r"市值[为是约]?\s*\$?([\d,]+\.?\d*)\s*[TB]",
    ],
}

_TICKER_CONTEXT_WINDOW = 300


def verify_report(
    content: str,
    ground_truth: list[dict],
    model: str | None = None,
) -> dict:
    """验证报告中的财务数字声明是否与注入的 ground truth 一致。"""
    truth_map = _build_truth_map(ground_truth)
    tickers = list(truth_map.keys())

    if not tickers:
        return _empty_result()

    claims = _extract_claims_regex(content, tickers)

    grounded = []
    hallucinated = []
    unverifiable = []

    for claim in claims:
        verdict = _compare_claim(claim, truth_map)
        claim["verdict"] = verdict
        if verdict == "grounded":
            grounded.append(claim)
        elif verdict == "hallucinated":
            hallucinated.append(claim)
        else:
            unverifiable.append(claim)

    total_verifiable = len(grounded) + len(hallucinated)
    credibility_score = len(grounded) / total_verifiable if total_verifiable > 0 else 1.0

    result = {
        "claims": claims,
        "grounded": grounded,
        "hallucinated": hallucinated,
        "unverifiable": unverifiable,
        "credibility_score": round(credibility_score, 3),
        "total_claims": len(claims),
    }
    result["markdown_section"] = format_credibility_section(result)
    return result


def _build_truth_map(ground_truth: list[dict]) -> dict[str, dict]:
    """构建 ticker -> metrics 的查找表。"""
    truth_map: dict[str, dict] = {}
    for item in ground_truth:
        ticker = (item.get("ticker") or "").upper()
        if not ticker:
            continue
        metrics: dict = {}
        if item.get("gross_margin") is not None:
            metrics["gross_margin"] = float(item["gross_margin"])
        if item.get("operating_margin") is not None:
            metrics["operating_margin"] = float(item["operating_margin"])
        if item.get("revenue_growth") is not None:
            metrics["revenue_growth"] = float(item["revenue_growth"])
        if item.get("pe_forward") is not None:
            metrics["pe_forward"] = float(item["pe_forward"])
        if item.get("pe_ttm") is not None:
            metrics["pe_ttm"] = float(item["pe_ttm"])
        if item.get("market_cap") is not None:
            metrics["market_cap"] = float(item["market_cap"])
        if metrics:
            truth_map[ticker] = metrics
    return truth_map


def _extract_claims_regex(content: str, tickers: list[str]) -> list[dict]:
    """正则提取报告中的财务数字声明。"""
    claims: list[dict] = []
    seen: set[tuple] = set()

    for ticker in tickers:
        positions = [m.start() for m in re.finditer(re.escape(ticker), content)]
        for pos in positions:
            window_start = max(0, pos - 50)
            window_end = min(len(content), pos + _TICKER_CONTEXT_WINDOW)
            window = content[window_start:window_end]

            for metric, patterns in _METRIC_PATTERNS.items():
                for pattern in patterns:
                    for m in re.finditer(pattern, window, re.IGNORECASE):
                        value_str = m.group(1).replace(",", "")
                        try:
                            value = float(value_str)
                        except ValueError:
                            continue

                        key = (ticker, metric, value)
                        if key in seen:
                            continue
                        seen.add(key)

                        if metric in ("gross_margin", "operating_margin", "revenue_growth"):
                            value = value / 100.0
                        if metric == "market_cap":
                            unit_match = re.search(r"(\d+\.?\d*)\s*T", m.group(0))
                            if unit_match:
                                value = value * 1e12
                            else:
                                value = value * 1e9

                        claims.append({
                            "ticker": ticker,
                            "metric": metric,
                            "value": value,
                            "text": m.group(0).strip(),
                        })

    return claims


def _compare_claim(claim: dict, truth_map: dict[str, dict]) -> str:
    """将单条声明与 ground truth 对比。"""
    ticker = claim["ticker"]
    metric = claim["metric"]
    claimed_value = claim["value"]

    if ticker not in truth_map:
        return "unverifiable"

    truth_metrics = truth_map[ticker]
    if metric not in truth_metrics:
        return "unverifiable"

    truth_value = truth_metrics[metric]

    if truth_value == 0:
        if abs(claimed_value) < 0.01:
            return "grounded"
        return "hallucinated"

    deviation = abs(claimed_value - truth_value) / abs(truth_value)
    if deviation <= _DEVIATION_THRESHOLD:
        return "grounded"
    return "hallucinated"


def format_credibility_section(result: dict) -> str:
    """格式化为 Markdown "数据可信度" 附录。"""
    if not result["claims"]:
        return ""

    score = result["credibility_score"]
    total = result["total_claims"]
    n_grounded = len(result["grounded"])
    n_hallucinated = len(result["hallucinated"])
    n_unverifiable = len(result["unverifiable"])

    if score >= 0.9:
        grade = "高"
        emoji = "✅"
    elif score >= 0.7:
        grade = "中"
        emoji = "⚠️"
    else:
        grade = "低"
        emoji = "❌"

    lines = [
        "",
        "---",
        "",
        f"### 数据可信度 {emoji}",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 可信度评分 | {score:.0%} ({grade}) |",
        f"| 可验证声明 | {n_grounded + n_hallucinated} / {total} |",
        f"| 与数据源一致 | {n_grounded} |",
        f"| 疑似偏差 (>20%) | {n_hallucinated} |",
        f"| 无法验证 | {n_unverifiable} |",
    ]

    if result["hallucinated"]:
        lines.append("")
        lines.append("**偏差声明：**")
        for h in result["hallucinated"][:5]:
            lines.append(f"- {h['ticker']} {h['metric']}: 报告称 `{h['text']}`")

    lines.append("")
    lines.append("> 可信度基于报告数字与 FMP 注入数据的对比。"
                 "偏差可能来自 LLM 幻觉或数据时效差异。")

    return "\n".join(lines)


def _empty_result() -> dict:
    return {
        "claims": [],
        "grounded": [],
        "hallucinated": [],
        "unverifiable": [],
        "credibility_score": 1.0,
        "total_claims": 0,
        "markdown_section": "",
    }
