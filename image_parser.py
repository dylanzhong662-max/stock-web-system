"""
截图持仓解析 — 使用 Qwen-VL（通义千问视觉模型）
支持富途、老虎、同花顺、雪球等中文券商截图
"""
import os
import base64
from typing import List, Optional
from openai import OpenAI
from json_utils import parse_json_array

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen-vl-max"

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)
    return _client


PARSE_PROMPT = """你是一个专业的金融数据提取助手。请仔细分析这张持仓截图，提取其中所有持仓记录。

请以 JSON 数组格式返回，每条持仓包含以下字段（无法识别的字段填 null）：
- asset: 资产名称（如"英伟达"、"苹果"、"比特币"等中文名或英文名）
- ticker: 股票代码（如 NVDA、AAPL、BTC-USD，港股如 00700）
- direction: 多空方向（"long" 或 "short"，默认 "long"）
- quantity: 持仓数量（股数/手数/币数）
- entry_price: 成本价/均价（数字）
- current_price: 最新价/现价（数字）
- cost_basis: 持仓市值/成本金额（数字，单位：元或美元）
- unrealized_pnl: 浮动盈亏金额（数字，亏损为负数）
- unrealized_pnl_pct: 浮动盈亏百分比（数字，如 -5.2 表示亏损5.2%）

只返回 JSON 数组，不要其他文字。示例格式：
[
  {
    "asset": "英伟达",
    "ticker": "NVDA",
    "direction": "long",
    "quantity": 10,
    "entry_price": 178.5,
    "current_price": 195.2,
    "cost_basis": 1785.0,
    "unrealized_pnl": 167.0,
    "unrealized_pnl_pct": 9.35
  }
]"""


def parse_image_to_positions(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """
    解析持仓截图，返回结构化持仓列表。
    image_bytes: 图片二进制内容
    mime_type: 图片 MIME 类型
    """
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    client = _get_client()
    try:
        resp = client.chat.completions.create(
            model=QWEN_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": PARSE_PROMPT},
                    ],
                }
            ],
            max_tokens=2000,
        )
        raw_text = resp.choices[0].message.content or ""
        positions = parse_json_array(raw_text)
        return {
            "success": True,
            "positions": positions,
            "raw_ocr": raw_text,
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "positions": [],
            "raw_ocr": None,
            "error": str(e),
        }


