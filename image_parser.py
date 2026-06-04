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


SYSTEM_PROMPT = """你是一个专业金融数据提取系统。你的唯一任务是从券商持仓截图中提取结构化数据。

核心约束：
- 只提取截图中明确可见的数据，禁止推测或编造任何数字
- 看不清或不确定的字段必须填 null
- 如果截图不是持仓页面（如新闻、聊天记录），返回空数组 []

市场判断规则：
- 富途/老虎/长桥 → 美股用英文 ticker（NVDA），港股用 5 位数字（00700.HK）
- 同花顺/东方财富/雪球 → A 股用 6 位数字（600519.SS），创业板（300xxx.SZ）
- 币安/OKX/Bybit → 加密货币用 XXX-USD 格式（BTC-USD）
- 如果无法确定市场，ticker 填 null"""

PARSE_PROMPT = """请提取截图中所有持仓记录，以 JSON 数组返回。

每条持仓字段（无法识别填 null）：
- asset: 资产名称（中文名或英文名）
- ticker: 标准代码（参照市场判断规则）
- direction: "long" 或 "short"（默认 "long"）
- quantity: 持仓数量
- entry_price: 成本价/均价
- current_price: 最新价/现价
- cost_basis: 持仓成本金额
- unrealized_pnl: 浮动盈亏金额（亏损为负）
- unrealized_pnl_pct: 浮动盈亏百分比（如 -5.2）

示例：
[
  {"asset": "英伟达", "ticker": "NVDA", "direction": "long", "quantity": 10, "entry_price": 178.5, "current_price": 195.2, "cost_basis": 1785.0, "unrealized_pnl": 167.0, "unrealized_pnl_pct": 9.35},
  {"asset": "恒生指数ETF", "ticker": "07200.HK", "direction": "short", "quantity": 500, "entry_price": 25.8, "current_price": 24.1, "cost_basis": 12900.0, "unrealized_pnl": 850.0, "unrealized_pnl_pct": 6.59}
]

只返回 JSON 数组，无其他文字。"""


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
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": PARSE_PROMPT},
                    ],
                },
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


