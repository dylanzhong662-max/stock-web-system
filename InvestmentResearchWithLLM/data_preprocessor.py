"""数据预处理 — 搜索结果压缩 + 行业筛选

在调用重推理模型前：
1. 用轻量模型(deepseek-v4-pro)批量压缩搜索结果，降低注意力稀释
2. 用 FMP sector/industry 字段过滤与目标行业不相关的 neglect 候选
"""
import asyncio
import json
import os

from llm_client import get_client, complete_chat

_SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL", "deepseek-v4-pro")
_BATCH_SIZE = 8


async def summarize_search_results(
    results: list[dict],
    model: str | None = None,
) -> list[dict]:
    """将搜索结果从 ~800 chars 压缩到 ~150 chars + 关键数字。

    Falls back to truncation on LLM failure.
    """
    if not results:
        return results

    model = model or _SUMMARIZER_MODEL
    batches = [results[i:i + _BATCH_SIZE] for i in range(0, len(results), _BATCH_SIZE)]
    tasks = [_compress_batch(batch, model) for batch in batches]
    batch_results = await asyncio.gather(*tasks, return_exceptions=True)

    compressed = []
    idx = 0
    for i, batch_result in enumerate(batch_results):
        batch = batches[i]
        if isinstance(batch_result, list) and len(batch_result) == len(batch):
            for j, item in enumerate(batch):
                out = dict(item)
                out["content"] = batch_result[j]
                compressed.append(out)
        else:
            for item in batch:
                out = dict(item)
                out["content"] = (item.get("content") or "")[:200]
                compressed.append(out)
        idx += len(batch)

    return compressed


async def _compress_batch(batch: list[dict], model: str) -> list[str]:
    """单次 LLM 调用压缩一批搜索结果。"""
    items_text = []
    for i, item in enumerate(batch):
        title = item.get("title", "")
        content = (item.get("content") or "")[:800]
        date = item.get("published_date", "")
        items_text.append(f"[{i}] {title}（{date}）\n{content}")

    prompt = (
        "你是数据压缩助手。将以下搜索结果各压缩为一句话摘要（≤150字），"
        "必须保留：关键数字（营收/利润率/增速/市值/出货量）、公司名、时间。"
        "去掉废话和重复信息。\n\n"
        "输出格式：JSON 数组，每项为压缩后的字符串，顺序与输入一致。\n"
        "只输出 JSON，不要其他文字。\n\n"
        + "\n\n".join(items_text)
    )

    try:
        raw = await complete_chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0,
        )
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) == len(batch):
            return [str(s)[:200] for s in parsed]
    except Exception:
        pass

    return [(item.get("content") or "")[:200] for item in batch]


def filter_neglect_by_sector(industry: str, candidates: list[dict]) -> list[dict]:
    """基于 FMP sector/industry 字段过滤不匹配目标行业的候选。

    保守策略：无法解析行业映射时不过滤。
    """
    if not candidates:
        return candidates

    target = _resolve_sector(industry)
    if not target:
        return candidates

    target_sector = target.get("sector", "").lower()
    target_industry = target.get("industry", "").lower()

    filtered = []
    for c in candidates:
        stock_sector = (c.get("sector") or "").lower()
        stock_industry = (c.get("industry") or "").lower()

        if not stock_sector:
            filtered.append(c)
            continue

        if target_sector and target_sector in stock_sector:
            if target_industry:
                if target_industry in stock_industry or not stock_industry:
                    filtered.append(c)
            else:
                filtered.append(c)
        elif _is_adjacent_sector(target_sector, stock_sector):
            filtered.append(c)

    return filtered if filtered else candidates


def _resolve_sector(industry: str) -> dict | None:
    """从 industry_seed_lists._SECTOR_MAP 解析目标 sector。"""
    try:
        from industry_seed_lists import _SECTOR_MAP, _INDUSTRY_ALIASES
    except ImportError:
        return None

    if industry in _SECTOR_MAP:
        return _SECTOR_MAP[industry]

    industry_lower = industry.lower()
    for alias, canonical in _INDUSTRY_ALIASES.items():
        if alias in industry_lower:
            return _SECTOR_MAP.get(canonical)

    for key in _SECTOR_MAP:
        if key in industry or industry in key:
            return _SECTOR_MAP[key]

    return None


def _is_adjacent_sector(target: str, stock: str) -> bool:
    """允许相邻行业通过（如 Technology 目标允许 Industrials 中的设备商）。"""
    adjacency = {
        "technology": {"industrials", "communication services"},
        "industrials": {"technology"},
        "healthcare": {"technology"},
        "energy": {"industrials", "utilities"},
        "consumer cyclical": {"technology", "industrials"},
    }
    return stock in adjacency.get(target, set())
