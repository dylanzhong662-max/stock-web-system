"""预测落库 + 结算 + 命中率统计

预测来源：
1. 主报告末尾的 ```prediction 代码块（容易被 max_tokens 截断）
2. 兜底：报告生成后独立调一次 v4-pro 抽取预测（稳定）

本模块负责：
1. 从报告文本中解析 ```prediction 代码块
2. 独立二次调用 LLM 生成预测 JSON（extract_via_llm）
3. 落入 predictions 表
4. 定时结算已到期预测：拉实际价格，算超额收益 / 命中率 / IC
"""
import re
import json
import asyncio
from datetime import datetime, timedelta
from typing import Iterable
import math

from database import SessionLocal
from models import Prediction

_PRED_BLOCK = re.compile(
    r"```prediction\s*(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL | re.IGNORECASE,
)

_VALID_DIRECTION = {"bullish", "bearish", "neutral"}
_DEFAULT_HORIZON = {"chain": 90, "company": 30, "portfolio": 30}


def extract(text: str) -> list[dict]:
    """从报告中解析所有 ```prediction``` 块，允许单对象或数组"""
    out: list[dict] = []
    for m in _PRED_BLOCK.finditer(text or ""):
        payload = m.group(1).strip()
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
        elif isinstance(obj, list):
            out.extend(p for p in obj if isinstance(p, dict))
    return out


def _normalize(
    raw: dict,
    report_type: str,
    cache_key: str,
    default_entry_price: float | None = None,
) -> dict | None:
    direction = str(raw.get("direction", "")).lower().strip()
    if direction not in _VALID_DIRECTION:
        return None

    horizon = raw.get("horizon_days")
    try:
        horizon = int(horizon) if horizon is not None else _DEFAULT_HORIZON.get(report_type, 30)
    except (TypeError, ValueError):
        horizon = _DEFAULT_HORIZON.get(report_type, 30)
    horizon = max(1, min(horizon, 365))  # clamp 1-365 天

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    conf = _f(raw.get("confidence"))
    if conf is not None:
        conf = max(0.0, min(1.0, conf))

    ticker = raw.get("ticker") or raw.get("symbol")
    ticker = str(ticker).upper().strip() if ticker else None

    return {
        "report_type": report_type,
        "cache_key": cache_key,
        "ticker": ticker,
        "direction": direction,
        "confidence": conf,
        "horizon_days": horizon,
        "target_price": _f(raw.get("target_price")),
        "entry_price": _f(raw.get("entry_price")) or default_entry_price,
        "rationale": (raw.get("rationale") or "")[:500] or None,
    }


def save_from_report(
    report_type: str,
    cache_key: str,
    content: str,
    entry_prices: dict[str, float] | None = None,
) -> int:
    """从报告 content 中抽出预测并落库，返回落库条数"""
    preds = extract(content)
    return _persist(preds, report_type, cache_key, entry_prices)


def _persist(
    preds: list[dict],
    report_type: str,
    cache_key: str,
    entry_prices: dict[str, float] | None = None,
) -> int:
    if not preds:
        return 0
    entry_prices = entry_prices or {}
    now = datetime.utcnow()
    rows: list[Prediction] = []

    for raw in preds:
        norm = _normalize(raw, report_type, cache_key)
        if not norm:
            continue
        if not norm["entry_price"] and norm["ticker"]:
            norm["entry_price"] = entry_prices.get(norm["ticker"])

        rows.append(Prediction(
            **norm,
            created_at=now,
            resolve_at=now + timedelta(days=norm["horizon_days"]),
        ))

    if not rows:
        return 0

    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()
    return len(rows)


# ---------------------------------------------------------------------------
# 二次调用：用轻量模型独立抽取预测 JSON
# ---------------------------------------------------------------------------

_EXTRACTOR_MODEL = "deepseek-v4-pro"  # 轻量快速，JSON 输出稳定

_EXTRACTOR_SYSTEM = """你是一个严格的 JSON 抽取器。
给定一份投研报告，你需要从中提取所有明确的**方向性持仓建议**，输出标准 JSON 数组。

输出规则（必须严格遵守）：
1. 只输出一个 JSON 数组，不要任何解释、不要 markdown 代码块
2. 每条预测字段：
   - ticker: 股票代码（大写，A 股用 600519.SH 格式）；行业类报告若无标的可省略
   - direction: bullish / bearish / neutral（"加仓"=bullish，"减仓/清仓"=bearish，"持有观望"=neutral）
   - confidence: 0.0-1.0，反映报告语气的确定性
   - horizon_days: 持仓类默认 30，产业链类默认 90，14-180 之间
   - rationale: 一句话理由，≤ 80 字
3. 只抽有明确方向的，"持有"但无方向倾向的不要输出
4. 不要为同一 ticker 输出多条，以最明确的那条为准
5. 如果报告中完全找不到可抽取的预测，输出 []
"""


async def extract_via_llm(
    content: str,
    report_type: str,
    cache_key: str,
    entry_prices: dict[str, float] | None = None,
    max_retries: int = 2,
) -> int:
    """独立二次调用抽取预测，规避主报告截断问题。返回落库条数。"""
    # 先尝试从报告原文抽（零成本，如果 LLM 主调用没截断就直接成功）
    inline = extract(content)
    if inline:
        return _persist(inline, report_type, cache_key, entry_prices)

    # 报告太长（R1 主调用可能截断预测块），截取后半段重点提取
    from llm_client import get_client
    tail = content[-6000:] if len(content) > 6000 else content

    prompt = (
        f"报告类型：{report_type}\n"
        f"报告 key：{cache_key}\n\n"
        f"以下是报告全文（或尾部），抽取所有方向性预测为 JSON 数组：\n\n{tail}"
    )

    for attempt in range(max_retries):
        try:
            client = get_client(_EXTRACTOR_MODEL)
            resp = await client.chat.completions.create(
                model=_EXTRACTOR_MODEL,
                messages=[
                    {"role": "system", "content": _EXTRACTOR_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0,
                max_tokens=1500,
            )
            raw = (resp.choices[0].message.content or "").strip()
            # 去掉可能的 markdown 包装
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            obj = json.loads(raw)
            if isinstance(obj, list):
                return _persist(obj, report_type, cache_key, entry_prices)
            if isinstance(obj, dict):
                return _persist([obj], report_type, cache_key, entry_prices)
        except (json.JSONDecodeError, Exception):
            if attempt == max_retries - 1:
                return 0
            await asyncio.sleep(1)
    return 0


# ---------------------------------------------------------------------------
# Resolver：到期结算
# ---------------------------------------------------------------------------

async def _fetch_price(ticker: str, as_of: datetime) -> float | None:
    """拉收盘价。优先用 FMP/AKShare（服务器上 yfinance 不稳定）。

    as_of 接近当前时间时直接用实时价格；历史价格走 AV 日线缓存。
    """
    import data_fetcher

    # 清理 ticker（去 .US 后缀等）
    clean = ticker.replace(".US", "").replace(".HK", "")

    # 方式1：如果 as_of 在最近 3 天内，直接用 FMP profile 的实时价格
    if (datetime.utcnow() - as_of).days <= 3:
        try:
            result = await data_fetcher.get_batch_stock_data([clean])
            data = result.get(clean, {})
            price = data.get("current_price")
            if price:
                return float(price)
        except Exception:
            pass

    # 方式2：AV/AKShare 日线取历史价格
    try:
        import pandas as pd
        series = await data_fetcher._get_av_daily(clean)
        if series:
            # 转为 Series，找 ≤ as_of 的最近一天
            s = pd.Series(series)
            s.index = pd.to_datetime(s.index)
            s = s.sort_index()
            mask = s.index <= pd.Timestamp(as_of)
            if mask.any():
                return float(s[mask].iloc[-1])
    except Exception:
        pass

    # 方式3：yfinance fallback
    def _sync():
        try:
            import yfinance as yf
            import pandas as pd
            yf_sym = data_fetcher._yf_ticker(ticker)
            start = (as_of - timedelta(days=10)).strftime("%Y-%m-%d")
            end = (as_of + timedelta(days=2)).strftime("%Y-%m-%d")
            df = yf.download(yf_sym, start=start, end=end, progress=False, auto_adjust=True)
            if df.empty:
                return None
            close = df["Close"].squeeze()
            close = close[close.index <= pd.Timestamp(as_of)]
            return float(close.iloc[-1]) if not close.empty else None
        except Exception:
            return None
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


def _score_hit(direction: str, realized_return: float, threshold: float = 0.02) -> bool:
    """方向命中判定：±2% 视为中性区间"""
    if direction == "bullish":
        return realized_return > threshold
    if direction == "bearish":
        return realized_return < -threshold
    # neutral：落在 ±threshold 内算命中
    return abs(realized_return) <= threshold


async def resolve_due(max_rows: int = 200, force: bool = False) -> dict:
    """结算预测。

    force=False: 只结算 resolve_at <= now 的到期预测（正常模式）
    force=True:  强制结算所有有 entry_price 的未结算预测（提前结算，用于验证系统）
    """
    db = SessionLocal()
    try:
        q = (
            db.query(Prediction)
            .filter(Prediction.resolved_at.is_(None))
            .filter(Prediction.entry_price.isnot(None))
            .filter(Prediction.ticker.isnot(None))
        )
        if not force:
            q = q.filter(Prediction.resolve_at <= datetime.utcnow())
        due: list[Prediction] = q.limit(max_rows).all()
    finally:
        db.close()

    if not due:
        return {"resolved": 0, "hit_rate": None, "message": "无可结算预测" + ("（所有预测尚未到期）" if not force else "")}

    resolved = 0
    hits = 0
    now = datetime.utcnow()

    for p in due:
        # force 模式用当前价格，正常模式用 resolve_at 时价格
        price_date = now if force else p.resolve_at
        cur_price = await _fetch_price(p.ticker, price_date)
        if cur_price is None or not p.entry_price or p.entry_price <= 0:
            continue

        rret = (cur_price - p.entry_price) / p.entry_price

        bench = await _fetch_price("SPY", price_date)
        bench_entry = await _fetch_price("SPY", p.created_at)
        bret = None
        excess = None
        if bench and bench_entry and bench_entry > 0:
            bret = (bench - bench_entry) / bench_entry
            excess = rret - bret

        hit = _score_hit(p.direction, rret)

        db = SessionLocal()
        try:
            row = db.query(Prediction).filter(Prediction.id == p.id).first()
            if row:
                row.resolved_at = now
                row.resolved_price = cur_price
                row.realized_return = rret
                row.benchmark_return = bret
                row.excess_return = excess
                row.hit = hit
                db.commit()
                resolved += 1
                if hit:
                    hits += 1
        finally:
            db.close()

    return {
        "resolved": resolved,
        "hit_rate": hits / resolved if resolved else None,
    }


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def performance(report_type: str | None = None, since_days: int = 365) -> dict:
    """命中率 / IC / 平均超额收益"""
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    db = SessionLocal()
    try:
        q = db.query(Prediction).filter(
            Prediction.resolved_at.isnot(None),
            Prediction.created_at >= cutoff,
        )
        if report_type:
            q = q.filter(Prediction.report_type == report_type)
        rows = q.all()
    finally:
        db.close()

    if not rows:
        return {
            "count": 0, "hit_rate": None, "avg_return": None,
            "avg_excess": None, "direction_ic": None,
        }

    hit_count = sum(1 for r in rows if r.hit)
    returns = [r.realized_return for r in rows if r.realized_return is not None]
    excess = [r.excess_return for r in rows if r.excess_return is not None]

    # Spearman-like 方向 IC：direction 编码为 ±1，与 realized_return 符号相关性
    def _dir_code(d: str) -> int:
        return {"bullish": 1, "bearish": -1, "neutral": 0}.get(d, 0)
    paired = [
        (_dir_code(r.direction), r.realized_return)
        for r in rows if r.realized_return is not None
    ]
    direction_ic = None
    if len(paired) >= 5:
        n = len(paired)
        dx = [p[0] for p in paired]
        dy = [p[1] for p in paired]
        mx, my = sum(dx)/n, sum(dy)/n
        num = sum((dx[i]-mx)*(dy[i]-my) for i in range(n))
        dx2 = sum((v-mx)**2 for v in dx)
        dy2 = sum((v-my)**2 for v in dy)
        denom = math.sqrt(dx2 * dy2) if dx2 > 0 and dy2 > 0 else 0
        if denom:
            direction_ic = num / denom

    return {
        "count": len(rows),
        "hit_rate": hit_count / len(rows),
        "avg_return": sum(returns) / len(returns) if returns else None,
        "avg_excess": sum(excess) / len(excess) if excess else None,
        "direction_ic": direction_ic,
    }


def list_recent(
    report_type: str | None = None,
    resolved: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(Prediction)
        if report_type:
            q = q.filter(Prediction.report_type == report_type)
        if resolved is True:
            q = q.filter(Prediction.resolved_at.isnot(None))
        elif resolved is False:
            q = q.filter(Prediction.resolved_at.is_(None))
        rows = q.order_by(Prediction.created_at.desc()).limit(limit).all()
    finally:
        db.close()

    return [
        {
            "id": r.id,
            "report_type": r.report_type,
            "cache_key": r.cache_key,
            "ticker": r.ticker,
            "direction": r.direction,
            "confidence": r.confidence,
            "horizon_days": r.horizon_days,
            "entry_price": r.entry_price,
            "target_price": r.target_price,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "resolve_at": r.resolve_at.isoformat() if r.resolve_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolved_price": r.resolved_price,
            "realized_return": r.realized_return,
            "benchmark_return": r.benchmark_return,
            "excess_return": r.excess_return,
            "hit": r.hit,
            "rationale": r.rationale,
        }
        for r in rows
    ]
