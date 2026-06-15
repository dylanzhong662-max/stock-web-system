"""Consistency Test — 同一产业链跑 N 次分析，比较预测一致性

核心假设：如果 LLM 对同一输入给出不同方向判断，信号为零。
一个真正的 alpha 信号应该是可复现的。

方法：
1. 用相同 prompt + data 跑 3 次 chain analysis
2. 从每次报告中抽取 predictions
3. 比较：
   - 方向一致性（3 次都是 bullish 才算一致）
   - Confidence 稳定性（标准差）
   - Ticker 覆盖一致性（是否每次都推荐同一组股票）
4. 输出 consistency_score (0-100)
"""
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

import predictions as pred_mod
from llm_client import get_client, resolve_model, has_reasoning, build_extra_params


_DEFAULT_N_RUNS = 3


async def _run_single_analysis(
    industry: str,
    prompt: str,
    model: str,
    run_id: int,
) -> list[dict]:
    """Run one chain analysis and extract predictions."""
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20000,
        temperature=0.3,
        stream=True,
        **build_extra_params(model),
    )
    chunks = []
    stream = await get_client(model).chat.completions.create(**kwargs)
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if has_reasoning(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
            continue
        if delta.content:
            chunks.append(delta.content)

    content = "".join(chunks)

    # Extract predictions from content
    preds = pred_mod.extract(content)

    # If inline extraction fails, try LLM extraction
    if not preds:
        try:
            from llm_client import get_client as _gc
            import re, json
            _model = "deepseek-v4-pro"
            tail = content[-6000:] if len(content) > 6000 else content
            prompt_extract = (
                f"从以下报告中抽取所有方向性预测为 JSON 数组：\n\n{tail}"
            )
            client = _gc(_model)
            resp = await client.chat.completions.create(
                model=_model,
                messages=[
                    {"role": "system", "content": pred_mod._EXTRACTOR_SYSTEM},
                    {"role": "user", "content": prompt_extract},
                ],
                temperature=0,
                max_tokens=1500,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            obj = json.loads(raw)
            if isinstance(obj, list):
                preds = obj
            elif isinstance(obj, dict):
                preds = [obj]
        except Exception:
            pass

    return [
        {
            "run_id": run_id,
            "ticker": (p.get("ticker") or "").upper(),
            "direction": (p.get("direction") or "").lower(),
            "confidence": p.get("confidence"),
            "rationale": p.get("rationale", ""),
        }
        for p in preds
        if p.get("ticker") and p.get("direction")
    ]


async def run_consistency_test(
    industry: str,
    n_runs: int = _DEFAULT_N_RUNS,
    model: Optional[str] = None,
) -> dict:
    """Run N analyses of the same industry and compare prediction consistency.

    This is expensive (N × full chain analysis call), so default is 3 runs.
    """
    from chain_analyzer import ChainAnalyzer

    model = resolve_model(model)
    analyzer = ChainAnalyzer()

    # Build prompt once (same data for all runs)
    all_results, fin_data, neglect_candidates = await analyzer._fetch_all_data(industry)
    prompt = analyzer._load_prompt(industry, all_results, fin_data, neglect_candidates)

    # Run N analyses in parallel
    tasks = [
        _run_single_analysis(industry, prompt, model, i)
        for i in range(n_runs)
    ]
    all_predictions = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out failed runs
    valid_runs = []
    for i, result in enumerate(all_predictions):
        if isinstance(result, list):
            valid_runs.append(result)
        else:
            valid_runs.append([])

    if not any(valid_runs):
        return {
            "industry": industry,
            "n_runs": n_runs,
            "model": model,
            "error": "All runs failed to produce predictions",
            "consistency_score": 0,
        }

    # Analyze consistency
    # 1. Ticker coverage: which tickers appear across runs
    ticker_counts: Counter = Counter()
    for run in valid_runs:
        for pred in run:
            ticker_counts[pred["ticker"]] += 1

    all_tickers = list(ticker_counts.keys())
    # Tickers that appear in ALL runs
    consistent_tickers = [t for t, c in ticker_counts.items() if c >= n_runs]
    # Tickers that appear in majority of runs
    majority_tickers = [t for t, c in ticker_counts.items() if c >= n_runs * 0.5]

    # 2. Direction consistency per ticker
    ticker_directions: defaultdict = defaultdict(list)
    ticker_confidences: defaultdict = defaultdict(list)
    for run in valid_runs:
        for pred in run:
            ticker_directions[pred["ticker"]].append(pred["direction"])
            if pred["confidence"] is not None:
                ticker_confidences[pred["ticker"]].append(pred["confidence"])

    direction_analysis = []
    for ticker in all_tickers:
        dirs = ticker_directions[ticker]
        confs = ticker_confidences[ticker]

        # Direction agreement
        if not dirs:
            continue
        most_common_dir = Counter(dirs).most_common(1)[0]
        agreement_pct = most_common_dir[1] / len(dirs)

        # Confidence stability
        conf_std = None
        conf_mean = None
        if len(confs) >= 2:
            conf_mean = sum(confs) / len(confs)
            conf_std = (sum((c - conf_mean) ** 2 for c in confs) / (len(confs) - 1)) ** 0.5

        direction_analysis.append({
            "ticker": ticker,
            "appearances": len(dirs),
            "directions": dirs,
            "consensus_direction": most_common_dir[0],
            "direction_agreement": round(agreement_pct, 2),
            "is_consistent": agreement_pct >= 1.0,
            "confidence_mean": round(conf_mean, 3) if conf_mean is not None else None,
            "confidence_std": round(conf_std, 3) if conf_std is not None else None,
        })

    # 3. Compute overall consistency score (0-100)
    # Component 1: ticker overlap (do the same stocks get picked?)
    if all_tickers:
        ticker_overlap_score = len(majority_tickers) / len(all_tickers) * 40
    else:
        ticker_overlap_score = 0

    # Component 2: direction agreement (for stocks that DO appear)
    agreements = [d["direction_agreement"] for d in direction_analysis]
    if agreements:
        direction_score = (sum(agreements) / len(agreements)) * 40
    else:
        direction_score = 0

    # Component 3: confidence stability
    conf_stds = [d["confidence_std"] for d in direction_analysis if d["confidence_std"] is not None]
    if conf_stds:
        avg_conf_std = sum(conf_stds) / len(conf_stds)
        confidence_score = max(0, (1 - avg_conf_std / 0.3)) * 20
    else:
        confidence_score = 10  # neutral if no confidence data

    consistency_score = round(ticker_overlap_score + direction_score + confidence_score, 1)

    # Reliable signals: tickers with 100% direction agreement across all runs
    reliable_signals = [
        d for d in direction_analysis
        if d["is_consistent"] and d["appearances"] >= n_runs
    ]

    return {
        "industry": industry,
        "n_runs": n_runs,
        "model": model,
        "timestamp": datetime.utcnow().isoformat(),
        "consistency_score": min(100, consistency_score),
        "total_unique_tickers": len(all_tickers),
        "consistent_tickers": len(consistent_tickers),
        "majority_tickers": len(majority_tickers),
        "ticker_analysis": direction_analysis,
        "reliable_signals": reliable_signals,
        "per_run_counts": [len(r) for r in valid_runs],
        "interpretation": _interpret_consistency(consistency_score, reliable_signals, n_runs),
        "actionable_signals": [
            {
                "ticker": s["ticker"],
                "direction": s["consensus_direction"],
                "confidence": s["confidence_mean"],
                "reliability": "high" if s["appearances"] >= n_runs else "medium",
            }
            for s in reliable_signals
        ],
    }


def _interpret_consistency(score: float, reliable: list, n_runs: int) -> str:
    parts = []
    if score >= 80:
        parts.append(f"信号一致性高 ({score:.0f}/100)——LLM 判断可复现")
    elif score >= 60:
        parts.append(f"信号一致性中等 ({score:.0f}/100)——部分标的判断稳定")
    elif score >= 40:
        parts.append(f"信号一致性低 ({score:.0f}/100)——LLM 输出噪声较大")
    else:
        parts.append(f"信号一致性极低 ({score:.0f}/100)——预测不可信赖")

    if reliable:
        parts.append(f"{len(reliable)} 个标的方向完全一致，可作为高置信信号")
    else:
        parts.append("无标的在所有运行中方向一致——当前无可执行信号")

    return "；".join(parts)
