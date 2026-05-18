"""监控清单系统

功能：
1. 从持仓分析报告中自动提取"关键变量监控清单"（第五章节）
2. 手动添加/删除/更新监控项
3. 在下次分析时，将现有监控项注入 prompt，让 LLM 评估变化

使用场景：
- 报告生成后自动调用 extract_from_report() 提取变量
- 用户手动 POST /api/watchlist 快速添加
- portfolio_research prompt 注入当前 watchlist context
"""
import re
import json
from datetime import datetime
from typing import Optional

from database import SessionLocal
from models import WatchItem
from llm_client import get_client


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_active(ticker: str | None = None, limit: int = 50) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(WatchItem).filter(WatchItem.active.is_(True))
        if ticker:
            q = q.filter(WatchItem.ticker == ticker.upper())
        rows = q.order_by(WatchItem.priority, WatchItem.created_at.desc()).limit(limit).all()
        return [_row_to_dict(r) for r in rows]
    finally:
        db.close()


def add_item(
    variable: str,
    ticker: str | None = None,
    frequency: str = "weekly",
    bullish_signal: str | None = None,
    bearish_signal: str | None = None,
    priority: int = 2,
    source: str = "manual",
    notes: str | None = None,
) -> dict:
    db = SessionLocal()
    try:
        # 去重：同 ticker + variable 不重复创建
        existing = db.query(WatchItem).filter(
            WatchItem.variable == variable,
            WatchItem.ticker == (ticker.upper() if ticker else None),
            WatchItem.active.is_(True),
        ).first()
        if existing:
            existing.bullish_signal = bullish_signal or existing.bullish_signal
            existing.bearish_signal = bearish_signal or existing.bearish_signal
            existing.priority = min(existing.priority, priority)
            existing.notes = notes or existing.notes
            db.commit()
            return _row_to_dict(existing)

        item = WatchItem(
            ticker=ticker.upper() if ticker else None,
            variable=variable,
            frequency=frequency,
            bullish_signal=bullish_signal,
            bearish_signal=bearish_signal,
            priority=priority,
            source=source,
            active=True,
            notes=notes,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return _row_to_dict(item)
    finally:
        db.close()


def remove_item(item_id: int) -> bool:
    db = SessionLocal()
    try:
        item = db.query(WatchItem).filter(WatchItem.id == item_id).first()
        if not item:
            return False
        item.active = False
        db.commit()
        return True
    finally:
        db.close()


def update_value(item_id: int, value: str) -> bool:
    db = SessionLocal()
    try:
        item = db.query(WatchItem).filter(WatchItem.id == item_id).first()
        if not item:
            return False
        item.current_value = value
        item.last_checked = datetime.utcnow()
        db.commit()
        return True
    finally:
        db.close()


def batch_add(items: list[dict], source: str = "portfolio_report") -> int:
    """批量添加（去重），返回新增条数"""
    count = 0
    for item in items:
        result = add_item(
            variable=item.get("variable", ""),
            ticker=item.get("ticker"),
            frequency=item.get("frequency", "weekly"),
            bullish_signal=item.get("bullish_signal"),
            bearish_signal=item.get("bearish_signal"),
            priority=item.get("priority", 2),
            source=source,
        )
        if result:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 从报告自动提取
# ---------------------------------------------------------------------------

_EXTRACTOR_MODEL = "deepseek-v4-pro"

_EXTRACT_SYSTEM = """你是一个结构化数据抽取器。从投研报告中提取"关键变量监控清单"。

输出格式：纯 JSON 数组，不要任何解释。每条：
{
  "ticker": "NVDA 或 null（宏观变量）",
  "variable": "变量名，如 FY26Q1 营收增速、VIX、10Y US Treasury",
  "frequency": "daily/weekly/monthly/quarterly",
  "bullish_signal": "看多触发条件，具体数字阈值",
  "bearish_signal": "看空触发条件，具体数字阈值",
  "priority": 1-3（1=最重要）
}

规则：
- 只抽有明确可量化阈值的变量（"关注增速"太模糊，"营收增速>25%"才合格）
- 宏观变量（VIX/DXY/利率）ticker 设为 null
- 最多抽 15 条，按重要性排序
- 如无可抽变量，输出 []
"""


async def extract_from_report(content: str) -> list[dict]:
    """从报告文本中用 LLM 抽取监控变量"""
    # 优先找第五章节
    section_match = re.search(
        r"###\s*五.*?监控清单.*?\n(.*?)(?=###|\Z)",
        content,
        re.DOTALL,
    )
    text = section_match.group(1) if section_match else content[-4000:]

    try:
        client = get_client(_EXTRACTOR_MODEL)
        resp = await client.chat.completions.create(
            model=_EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": f"从以下报告中提取监控变量：\n\n{text[:4000]}"},
            ],
            temperature=0,
            max_tokens=2000,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        items = json.loads(raw)
        if isinstance(items, list):
            return items
    except Exception:
        pass
    return []


async def extract_and_save(content: str) -> int:
    """从报告抽取并落库，返回新增条数"""
    items = await extract_from_report(content)
    if not items:
        return 0
    return batch_add(items, source="portfolio_report")


# ---------------------------------------------------------------------------
# Prompt 注入：在持仓分析时提供上次的监控清单
# ---------------------------------------------------------------------------

def build_watchlist_context() -> str:
    """构建注入到持仓分析 prompt 的监控上下文"""
    items = list_active(limit=20)
    if not items:
        return ""

    lines = ["**当前监控清单（上次分析提取，请评估变化情况）：**", ""]
    for item in items:
        ticker_tag = f"[{item['ticker']}] " if item['ticker'] else "[宏观] "
        check_info = ""
        if item.get("current_value"):
            check_info = f"（最近值：{item['current_value']}）"
        lines.append(
            f"- {ticker_tag}**{item['variable']}** "
            f"| 频率：{item['frequency']} "
            f"| 看多：{item.get('bullish_signal', 'N/A')} "
            f"| 看空：{item.get('bearish_signal', 'N/A')}"
            f"{check_info}"
        )

    lines.append("")
    lines.append("> 请在逐仓分析中评估上述变量的最新状态，并在第五章更新/替换过时的监控项。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _row_to_dict(r: WatchItem) -> dict:
    return {
        "id": r.id,
        "ticker": r.ticker,
        "variable": r.variable,
        "frequency": r.frequency,
        "bullish_signal": r.bullish_signal,
        "bearish_signal": r.bearish_signal,
        "current_value": r.current_value,
        "last_checked": r.last_checked.isoformat() if r.last_checked else None,
        "source": r.source,
        "priority": r.priority,
        "active": r.active,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "notes": r.notes,
    }
