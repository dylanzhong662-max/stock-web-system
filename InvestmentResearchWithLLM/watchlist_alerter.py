"""监控清单预警 → 飞书推送

定时检查 watchlist 中每个活跃项的当前值，对比 bullish/bearish 阈值，
触发时发送飞书 webhook 通知。

触发方式：
1. API 手动触发：POST /api/research/watchlist/check-alerts
2. cron 定时（推荐）：每天 09:30 / 15:30 CST 各跑一次
"""
import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from database import SessionLocal
from models import WatchItem

logger = logging.getLogger(__name__)

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")

# 宏观指标关键词 → (FMP symbol, is_index)
# is_index=True 用 /stable/historical-price-eod/light，False 用 /stable/profile
_MACRO_KEYWORDS = [
    (["vix", "恐慌", "波动率"], "^VIX", True),
    (["10y", "treasury", "国债收益率", "十年期"], "^TNX", True),
    (["dxy", "美元指数", "dollar index"], "^DXY", True),
    (["spy", "标普500"], "SPY", False),
    (["qqq", "纳斯达克"], "QQQ", False),
    (["gold", "黄金", "xau"], "GLD", False),
    (["oil", "原油", "wti", "crude"], "USO", False),
    (["btc", "比特币", "bitcoin"], "BTCUSD", False),
]


def _parse_threshold(signal_text: str | None) -> Optional[float]:
    """从阈值描述中提取数字。支持 '>4.5%' / '<20' / '突破 150' 等格式"""
    if not signal_text:
        return None
    numbers = re.findall(r"[-+]?\d*\.?\d+", signal_text)
    if numbers:
        return float(numbers[0])
    return None


def _check_direction(signal_text: str | None) -> str:
    """判断阈值方向：'>' 还是 '<'"""
    if not signal_text:
        return ">"
    if any(k in signal_text for k in ["<", "低于", "跌破", "下降"]):
        return "<"
    return ">"


def _resolve_symbol(item: dict) -> tuple[Optional[str], bool]:
    """返回 (symbol, is_index)"""
    ticker = item.get("ticker")
    if ticker:
        return ticker.upper(), False

    variable = (item.get("variable") or "").lower()
    for keywords, symbol, is_index in _MACRO_KEYWORDS:
        if any(kw in variable for kw in keywords):
            return symbol, is_index
    return None, False


async def _fetch_current_value(item: dict) -> Optional[float]:
    """根据监控项获取当前值。FMP historical（指数） / FMP profile（股票）"""
    symbol, is_index = _resolve_symbol(item)
    if not symbol:
        return None

    fmp_key = os.environ.get("FMP_API_KEY", "")
    fmp_base = "https://financialmodelingprep.com"

    if not fmp_key:
        return None

    async with httpx.AsyncClient(timeout=12) as client:
        # 指数用 historical-price-eod/light
        if is_index:
            try:
                today = datetime.utcnow().strftime("%Y-%m-%d")
                week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
                resp = await client.get(
                    f"{fmp_base}/stable/historical-price-eod/light",
                    params={"symbol": symbol, "from": week_ago, "to": today, "apikey": fmp_key},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return float(data[0]["price"])
            except Exception:
                pass
        else:
            # 股票/ETF 用 /stable/profile
            try:
                resp = await client.get(
                    f"{fmp_base}/stable/profile",
                    params={"symbol": symbol, "apikey": fmp_key},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data and data[0].get("price"):
                        return float(data[0]["price"])
            except Exception:
                pass

    return None


def _is_triggered(current: float, signal_text: str | None) -> bool:
    """判断当前值是否触发了阈值"""
    threshold = _parse_threshold(signal_text)
    if threshold is None:
        return False
    direction = _check_direction(signal_text)
    if direction == ">":
        return current > threshold
    else:
        return current < threshold


async def check_alerts() -> dict:
    """检查所有活跃监控项，返回触发的预警列表"""
    db = SessionLocal()
    try:
        items = db.query(WatchItem).filter(WatchItem.active.is_(True)).all()
        item_dicts = [
            {
                "id": r.id,
                "ticker": r.ticker,
                "variable": r.variable,
                "frequency": r.frequency,
                "bullish_signal": r.bullish_signal,
                "bearish_signal": r.bearish_signal,
                "current_value": r.current_value,
                "priority": r.priority,
            }
            for r in items
        ]
    finally:
        db.close()

    if not item_dicts:
        return {"alerts": [], "checked": 0, "triggered": 0}

    # 并行获取所有监控项当前值（每项 15s 超时）
    async def _safe_fetch(item):
        try:
            return await asyncio.wait_for(_fetch_current_value(item), timeout=15)
        except (asyncio.TimeoutError, Exception):
            return None

    values = await asyncio.gather(*[_safe_fetch(item) for item in item_dicts])

    alerts = []
    checked = 0
    skipped = []

    for item, value in zip(item_dicts, values):
        if value is None:
            skipped.append(item["variable"])
            continue

        checked += 1

        # 更新 current_value
        db = SessionLocal()
        try:
            row = db.query(WatchItem).filter(WatchItem.id == item["id"]).first()
            if row:
                row.current_value = str(round(value, 4))
                row.last_checked = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        # 检查 bullish 触发
        if _is_triggered(value, item.get("bullish_signal")):
            alerts.append({
                "item_id": item["id"],
                "ticker": item["ticker"],
                "variable": item["variable"],
                "signal_type": "bullish",
                "threshold": item["bullish_signal"],
                "current_value": value,
                "priority": item["priority"],
            })

        # 检查 bearish 触发
        if _is_triggered(value, item.get("bearish_signal")):
            alerts.append({
                "item_id": item["id"],
                "ticker": item["ticker"],
                "variable": item["variable"],
                "signal_type": "bearish",
                "threshold": item["bearish_signal"],
                "current_value": value,
                "priority": item["priority"],
            })

    result = {"alerts": alerts, "checked": checked, "triggered": len(alerts)}
    if skipped:
        result["skipped"] = skipped
    return result


def _build_feishu_message(alerts: list[dict]) -> dict:
    """构建飞书富文本消息"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = []

    content.append([{"tag": "text", "text": f"触发时间: {now}  |  预警数: {len(alerts)}"}])
    content.append([{"tag": "text", "text": "\n"}])

    for alert in sorted(alerts, key=lambda x: x["priority"]):
        priority_icon = {1: "🔴", 2: "🟡", 3: "⚪"}.get(alert["priority"], "⚪")
        signal_icon = "📈" if alert["signal_type"] == "bullish" else "📉"
        ticker_tag = f"[{alert['ticker']}]" if alert["ticker"] else "[宏观]"

        content.append([{
            "tag": "text",
            "text": (
                f"  {priority_icon}{signal_icon} {ticker_tag} {alert['variable']}\n"
                f"     当前值: {alert['current_value']:.4g}  |  阈值: {alert['threshold']}"
            ),
        }])

    content.append([{"tag": "text", "text": "\n"}])
    content.append([{"tag": "text", "text": "以上为自动监控预警，请结合最新市场情况判断。"}])

    return {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {
            "title": f"⚠️ 投研监控预警  {datetime.now().strftime('%m-%d %H:%M')}",
            "content": content,
        }}},
    }


async def send_feishu_alert(alerts: list[dict], webhook_url: str | None = None) -> bool:
    """发送预警到飞书"""
    url = webhook_url or FEISHU_WEBHOOK_URL
    if not url:
        logger.warning("FEISHU_WEBHOOK_URL 未配置，跳过推送")
        return False

    if not alerts:
        return True

    msg = _build_feishu_message(alerts)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=msg)
            result = resp.json()
            ok = result.get("StatusCode") == 0 or result.get("code") == 0
            if ok:
                logger.info(f"飞书预警推送成功，{len(alerts)} 条")
            else:
                logger.error(f"飞书推送失败: {result}")
            return ok
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")
        return False


async def run_check_and_notify(webhook_url: str | None = None) -> dict:
    """完整流程：检查 → 触发 → 推送飞书"""
    result = await check_alerts()

    if result["alerts"]:
        sent = await send_feishu_alert(result["alerts"], webhook_url)
        result["feishu_sent"] = sent
    else:
        result["feishu_sent"] = False
        result["message"] = "无预警触发"

    return result
