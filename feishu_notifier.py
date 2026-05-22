"""
holderAndAction 飞书推送器
在每日分析完成后，自动推送持仓状态 + DeepSeek R1 调仓建议到飞书群。

运行方式：
    python feishu_notifier.py             # 读数据库持仓，生成建议并推送
    python feishu_notifier.py --test      # 发送连通性测试消息
"""
import os
import json
import argparse
from datetime import datetime

import requests

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")


def send(message: dict, webhook_url: str) -> bool:
    try:
        resp = requests.post(
            webhook_url,
            json=message,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        result = resp.json()
        ok = result.get("StatusCode") == 0 or result.get("code") == 0
        print("飞书发送" + ("成功" if ok else f"失败: {result}"))
        return ok
    except Exception as e:
        print(f"飞书发送异常: {e}")
        return False


def _row(label: str, value) -> list:
    return [
        {"tag": "text", "un_escape": True, "text": f"{label}: "},
        {"tag": "text", "text": str(value) if value is not None else "N/A"},
    ]


def _urgency_icon(urgency: str) -> str:
    return {"urgent": "🔴", "normal": "🟡", "low": "⚪"}.get(urgency, "⚪")


def _action_icon(action: str) -> str:
    icons = {"持有": "✋", "加仓": "📈", "减仓": "📉", "平仓": "🚫", "观察": "👀"}
    return icons.get(action, "")


def build_advice_message(positions: list, advice: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = []

    # 持仓概览
    content.append([{"tag": "text", "text": f"生成时间: {today}  |  持仓数: {len(positions)}"}])
    content.append([{"tag": "text", "text": "\n"}])

    # 持仓盈亏摘要
    if positions:
        content.append([{"tag": "text", "text": "【当前持仓】"}])
        for p in positions:
            asset = p.get("asset", "?")
            ticker = p.get("ticker", "")
            direction = "多" if p.get("direction") == "long" else "空"
            pnl_pct = p.get("unrealized_pnl_pct")
            pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"
            pnl_icon = "📈" if (pnl_pct or 0) >= 0 else "📉"
            content.append([{"tag": "text", "text": f"  {pnl_icon} {asset}({ticker}) {direction}  浮盈亏: {pnl_str}"}])

    content.append([{"tag": "text", "text": "\n"}])

    # 总体概述
    summary = advice.get("summary", "")
    if summary:
        content.append([{"tag": "text", "text": f"【市场概述】\n{summary}"}])
        content.append([{"tag": "text", "text": "\n"}])

    # 逐资产建议
    recommendations = advice.get("recommendations", [])
    if recommendations:
        content.append([{"tag": "text", "text": "【调仓建议】"}])
        for r in recommendations:
            icon = _urgency_icon(r.get("urgency", "low"))
            action_icon = _action_icon(r.get("current_action", ""))
            asset = r.get("asset", "?")
            ticker = r.get("ticker", "")
            action = r.get("current_action", "观察")
            reason = r.get("reason", "")
            content.append([{"tag": "text", "text": f"  {icon} {action_icon} {asset}({ticker}) → {action}"}])
            if reason:
                content.append([{"tag": "text", "text": f"     {reason}"}])

    content.append([{"tag": "text", "text": "\n"}])

    # 风险提示
    risk_notes = advice.get("risk_notes", [])
    if risk_notes:
        content.append([{"tag": "text", "text": "【风险提示】"}])
        for note in risk_notes:
            content.append([{"tag": "text", "text": f"  ⚠ {note}"}])

    content.append([{"tag": "text", "text": "\n以上为 AI 生成建议，仅供参考，不构成投资建议。"}])

    return {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {
            "title": f"📊 持仓调仓建议  {datetime.now().strftime('%Y-%m-%d')}",
            "content": content,
        }}},
    }


def run_and_push(webhook_url: str):
    """读取数据库持仓 → 生成调仓建议 → 推送飞书"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # 读持仓（通过内部 API 或直接访问数据库）
    try:
        from database import SessionLocal
        from models import Position
        import price_fetcher
        import signal_reader
        import advisor as advisor_module

        db = SessionLocal()
        positions_orm = db.query(Position).filter(Position.status == "open").all()
        db.close()

        if not positions_orm:
            print("当前无开仓持仓，跳过推送")
            return

        from pnl import calc_pnl

        positions_data = []
        for p in positions_orm:
            current_price = price_fetcher.get_current_price(p.asset)
            pnl_pct = None
            if current_price and p.entry_price:
                _, pnl_pct = calc_pnl(p.direction, p.entry_price, current_price, p.quantity)
            positions_data.append({
                "asset": p.asset,
                "ticker": p.ticker,
                "direction": p.direction,
                "quantity": p.quantity,
                "entry_price": p.entry_price,
                "current_price": current_price,
                "cost_basis": p.cost_basis_usd,
                "stop_loss": p.stop_loss,
                "profit_target": p.profit_target,
                "unrealized_pnl_pct": pnl_pct,
            })

        print(f"读取到 {len(positions_data)} 条持仓，正在调用 DeepSeek R1 生成建议...")
        signals = signal_reader.read_all_signals()
        advice = advisor_module.generate_advice(positions_data, signals)

        msg = build_advice_message(positions_data, advice)
        send(msg, webhook_url)

    except Exception as e:
        print(f"推送流程异常: {e}")
        error_msg = {
            "msg_type": "text",
            "content": {"text": f"⚠ holderAndAction 飞书推送失败\n错误: {e}\n时间: {datetime.now().isoformat()}"},
        }
        send(error_msg, webhook_url)


def main():
    parser = argparse.ArgumentParser(description="持仓建议飞书推送器")
    parser.add_argument("--test", action="store_true", help="发送连通性测试消息")
    parser.add_argument("--webhook", default="", help="飞书 Webhook URL（覆盖环境变量）")
    args = parser.parse_args()

    webhook_url = args.webhook or FEISHU_WEBHOOK_URL
    if not webhook_url:
        print("错误: 请设置 FEISHU_WEBHOOK_URL 环境变量")
        return

    if args.test:
        msg = {
            "msg_type": "text",
            "content": {"text": f"✅ holderAndAction 飞书连接测试 OK\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
        }
        send(msg, webhook_url)
        return

    run_and_push(webhook_url)


if __name__ == "__main__":
    main()
