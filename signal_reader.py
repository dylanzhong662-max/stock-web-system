import json
import os
import re
from datetime import datetime
from typing import Optional, Dict

# 指向大模型金融分析项目根目录，部署时通过环境变量覆盖
FINANCE_ROOT = os.environ.get(
    "FINANCE_PROJECT_ROOT",
    "/opt/finance-analysis"
)

SIGNAL_FILES = {
    "GOLD":       "outputs/gold_api_output.txt",
    "SILVER":     "outputs/slv_api_output.txt",
    "COPPER":     "outputs/copx_api_output.txt",
    "RARE_EARTH": "outputs/remx_api_output.txt",
    "OIL":        "outputs/uso_api_output.txt",
    "BTC":        "outputs/btc_api_output.txt",
    "GOOGL":      "outputs/googl_api_output.txt",
    "MSFT":       "outputs/msft_api_output.txt",
    "NVDA":       "outputs/nvda_api_output.txt",
    "AAPL":       "outputs/aapl_api_output.txt",
    "META":       "outputs/meta_api_output.txt",
    "AMZN":       "outputs/amzn_api_output.txt",
}

SCRIPT_MAP = {
    "GOLD":       ("gold_analysis.py",      None),
    "BTC":        ("btc_analysis.py",       None),
    "SILVER":     ("tech_stock_analysis.py","SLV"),
    "COPPER":     ("tech_stock_analysis.py","COPX"),
    "RARE_EARTH": ("tech_stock_analysis.py","REMX"),
    "OIL":        ("tech_stock_analysis.py","USO"),
    "GOOGL":      ("tech_stock_analysis.py","GOOGL"),
    "MSFT":       ("tech_stock_analysis.py","MSFT"),
    "NVDA":       ("tech_stock_analysis.py","NVDA"),
    "AAPL":       ("tech_stock_analysis.py","AAPL"),
    "META":       ("tech_stock_analysis.py","META"),
    "AMZN":       ("tech_stock_analysis.py","AMZN"),
}


def parse_json_from_text(text: str) -> Optional[Dict]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start: i + 1])
                    except Exception:
                        break
    return None


def get_file_mtime(asset: str) -> Optional[str]:
    filename = SIGNAL_FILES.get(_normalize_asset_key(asset), "")
    filepath = os.path.join(FINANCE_ROOT, filename)
    try:
        mtime = os.path.getmtime(filepath)
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def read_signal(asset: str) -> Optional[Dict]:
    filename = SIGNAL_FILES.get(_normalize_asset_key(asset))
    if not filename:
        return None
    filepath = os.path.join(FINANCE_ROOT, filename)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return parse_json_from_text(f.read())
    except Exception:
        return None


def _normalize_asset_key(asset: str) -> str:
    """将 ticker/asset 字符串规范化为 SIGNAL_FILES 中的 key。
    例: 'AAPL.US' → 'AAPL', 'GOOG.US' → 'GOOGL', '518800.SH' → '518800.SH'
    """
    s = asset.upper().strip()
    for suffix in (".US", ".HK", ".SS", ".SZ", ".SH"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # 直接命中
    if s in SIGNAL_FILES:
        return s
    # GOOG → GOOGL 别名
    ALIAS = {"GOOG": "GOOGL"}
    return ALIAS.get(s, s)


def extract_signal_summary(asset: str) -> Optional[Dict]:
    asset = _normalize_asset_key(asset)
    raw = read_signal(asset)
    if not raw:
        return None
    analyses = raw.get("asset_analysis", [])
    if not analyses:
        return None
    first = analyses[0]
    return {
        "asset": asset,
        "action": first.get("action", "no_trade"),
        "bias_score": first.get("bias_score"),
        "regime": first.get("regime"),
        "entry_zone": first.get("entry_zone"),
        "stop_loss": first.get("stop_loss"),
        "profit_target": first.get("profit_target"),
        "risk_reward_ratio": first.get("risk_reward_ratio"),
        "estimated_holding_weeks": first.get("estimated_holding_weeks"),
        "position_size_pct": first.get("position_size_pct"),
        "invalidation_condition": first.get("invalidation_condition"),
        "justification": first.get("justification"),
        "market_sentiment": raw.get("overall_market_sentiment") or raw.get("macro_environment"),
        "analysis_date": get_file_mtime(asset),
        "raw": raw,
    }


def read_all_signals() -> Dict[str, Optional[Dict]]:
    return {asset: extract_signal_summary(asset) for asset in SIGNAL_FILES}


def read_market_scan() -> Optional[Dict]:
    filepath = os.path.join(FINANCE_ROOT, "market_scan_output.json")
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
