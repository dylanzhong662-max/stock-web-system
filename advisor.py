"""
持仓调整建议 — 多模型支持
  - DeepSeek 系列（deepseek-reasoner / deepseek-chat）：直连 https://api.deepseek.com
  - GPT / Gemini 等：通过 CloseAI 代理 https://api.openai-proxy.org/v1

环境变量：
  DEEPSEEK_API_KEY  — DeepSeek API Key（直连）
  CLOSEAI_API_KEY   — CloseAI API Key（GPT/Gemini 代理）
  DEEPSEEK_BASE_URL — 可选，默认 https://api.deepseek.com
  CLOSEAI_BASE_URL  — 可选，默认 https://api.openai-proxy.org/v1
"""
import os
import json
import asyncio
from typing import List, Optional
from openai import OpenAI
from json_utils import parse_json_object

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_CLOSEAI_BASE_URL = "https://api.openai-proxy.org/v1"
_DEFAULT_MODEL = "deepseek-reasoner"

_DEEPSEEK_PREFIXES = ("deepseek-",)

_clients: dict = {}


def _is_deepseek(model: str) -> bool:
    return any(model.startswith(p) for p in _DEEPSEEK_PREFIXES)


def _get_client(model: str) -> OpenAI:
    if _is_deepseek(model):
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", _DEEPSEEK_BASE_URL)
        cache_key = f"deepseek:{api_key}"
    else:
        api_key = os.environ.get("CLOSEAI_API_KEY") or os.environ.get("LLM_API_KEY", "")
        base_url = os.environ.get("CLOSEAI_BASE_URL", _CLOSEAI_BASE_URL)
        cache_key = f"closeai:{api_key}"

    if cache_key not in _clients:
        _clients[cache_key] = OpenAI(api_key=api_key, base_url=base_url)
    return _clients[cache_key]


def _get_model(model_override: Optional[str] = None) -> str:
    if model_override:
        return model_override
    return os.environ.get("DEEPSEEK_MODEL", _DEFAULT_MODEL)


SYSTEM_PROMPT = """你是对冲基金级别的资产组合管理顾问，专注于量化策略与风险管理。全部使用中文输出。

你会收到用户当前持仓和 LLM 信号系统的分析结果，你的任务是输出可直接执行的具体操作指引。

━━━ 变量定义 ━━━
- total_capital = 用户所有持仓的 cost_basis 之和（若为空则无法计算，用单资产估算）
- suggested_quantity = position_size_pct × total_capital ÷ 当前价（取整）

━━━ 金字塔加仓法（核心规则）━━━

原则：首批仓位最大，逐批递减，仅在浮盈后加仓。

| bias_score | 分批方案（占 position_size_pct 比例 → 触发条件） |
|---|---|
| 0.50-0.59 | 40% 当前价 → 30% +2% → 30% +4% |
| 0.60-0.69 | 50% 当前价 → 30% +3% → 20% +5%（移动止损至成本价） |
| 0.70-0.79 | 60% 当前价 → 40% +2% |
| ≥ 0.80 | 70% 当前价 → 30% +2%（移动止损至成本价） |

加仓（已有持仓浮盈 > 0）：加仓量 = 当前持仓的 30-50%（不超首批），加仓后止损上移至前一批入场价。
减仓：分 2 次各 50%；到达目标价先减 50% 锁利，保留 50% 让利润奔跑。

━━━ 信号处理规则 ━━━
- action=short 但持仓为 long → 平仓，urgency=urgent
- action=long 但持仓为 short → 平仓，urgency=urgent
- 止损已触及：long 仓 current_price ≤ stop_loss，或 short 仓 current_price ≥ stop_loss → 立即平仓，urgency=urgent
- 信号标记为 ⚠️STALE → urgency 强制降级为 low，reason 必须注明"信号已过期（>48h），请核实后再操作"
- 无信号的持仓 → current_action="持有"，reason 注明"信号缺失，维持原计划"

━━━ 宏观风险调整 ━━━
当收到 Polymarket 宏观风险信号时，数据中会包含 position_multiplier（仓位系数，如 0.5 表示减半）：
- regime=defensive：所有建仓量 × position_multiplier（默认 0.5），止损收紧 1 ATR，非 urgent 新建仓改为 low
- regime=hawkish：growth 类建仓量 × position_multiplier（默认 0.7），偏好 value 类资产
- regime=neutral 或无数据：position_multiplier=1.0，不调整

━━━ new_opportunities ━━━
遍历所有信号，找出 action=long 且 bias≥0.55 但用户尚无持仓的资产，按金字塔法给出分批计划。

━━━ 输出格式（严格 JSON）━━━
{
  "summary": "总体市场环境与持仓状态概述（100字以内）",
  "recommendations": [
    {
      "asset": "资产名称",
      "ticker": "代码",
      "current_action": "建仓/加仓/减仓/平仓/持有/观察",
      "urgency": "urgent/normal/low",
      "reason": "操作理由（60字以内）",
      "entry_plan": {
        "entry_zone": "入场价格区间",
        "batches": [
          {"batch": 1, "pct_of_total": 0.5, "trigger": "触发条件", "quantity": "约X股"}
        ],
        "total_position_size_pct": 0.3,
        "total_suggested_quantity": "合计约X股 / $XXXX",
        "order_type": "限价单/市价单"
      },
      "exit_plan": {
        "stop_loss": 175.0,
        "stop_loss_note": "止损说明",
        "profit_target": 220.0,
        "target_note": "止盈说明"
      },
      "if_already_holding": "已持仓时的具体操作",
      "if_no_position": "未持仓时的分批计划"
    }
  ],
  "new_opportunities": [
    {
      "asset": "资产名称",
      "ticker": "代码",
      "bias_score": 0.65,
      "entry_zone": "入场区间",
      "stop_loss": 0.0,
      "profit_target": 0.0,
      "position_size_pct": 0.3,
      "batches": [{"batch": 1, "pct_of_total": 0.5, "trigger": "条件", "quantity": "约X股"}],
      "reason": "建仓理由"
    }
  ],
  "risk_notes": ["风险提示1", "风险提示2"]
}

注意：
- current_action 为"持有"或"观察"时，entry_plan 可省略
- 确保所有数字为数值类型，不要加引号
- 只返回 JSON，不要包裹在 markdown 代码块中"""


def _fetch_risk_overlay() -> str:
    """同步获取 Polymarket 风险 overlay（阻塞但有超时保护）"""
    try:
        import sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if os.path.join(_here, "InvestmentResearchWithLLM") not in sys.path:
            sys.path.insert(0, os.path.join(_here, "InvestmentResearchWithLLM"))
        from rag_client import get_risk_overlay, fmt_risk_overlay

        loop = asyncio.new_event_loop()
        try:
            overlay = loop.run_until_complete(get_risk_overlay(days=7))
        finally:
            loop.close()
        return fmt_risk_overlay(overlay)
    except Exception:
        return ""


def generate_advice(positions: List[dict], signals: dict, model_override: Optional[str] = None) -> dict:
    """
    positions: 持仓列表（来自截图解析或数据库）
    signals: 各资产信号摘要 {asset: signal_summary}
    """
    # 获取 Polymarket 宏观风险环境
    risk_context = _fetch_risk_overlay()

    clean_signals = {}
    stale_assets = []
    for k, sig in signals.items():
        if not sig or not sig.get("action"):
            continue
        clean = {key: v for key, v in sig.items() if key != "raw"}
        if sig.get("is_stale"):
            clean["⚠️STALE"] = True
            stale_assets.append(k)
        clean_signals[k] = clean

    signals_section = json.dumps(clean_signals, ensure_ascii=False, indent=2)

    parts = [
        f"当前持仓：\n{json.dumps(positions, ensure_ascii=False, indent=2)}",
        f"大模型金融分析系统最新信号：\n{signals_section}",
    ]
    if stale_assets:
        parts.append(
            f"⚠️ 过期警告：以下资产信号已超过 48 小时未更新，时效性存疑：{', '.join(stale_assets)}。"
            f"对这些资产的操作 urgency 必须降级为 low。"
        )
    if risk_context:
        parts.append(f"Polymarket 宏观风险环境：\n{risk_context}")
    parts.append("请给出调仓建议。")

    user_content = "\n\n".join(parts)

    model = _get_model(model_override)
    client = _get_client(model)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=16000,
        )
        raw_text = resp.choices[0].message.content or ""
        thinking = ""
        # DeepSeek R1 的推理过程在 reasoning_content 字段
        if hasattr(resp.choices[0].message, "reasoning_content"):
            thinking = resp.choices[0].message.reasoning_content or ""

        parsed = _parse_advice(raw_text)
        parsed["raw_thinking"] = thinking
        parsed["model_used"] = model
        return parsed

    except Exception as e:
        return {
            "summary": f"生成建议失败：{e}",
            "recommendations": [],
            "risk_notes": [f"API 调用失败，请检查 LLM_API_KEY 配置（当前模型：{model}）"],
            "raw_thinking": None,
        }


def _parse_advice(text: str) -> dict:
    result = parse_json_object(text)
    if result is not None:
        return result
    return {
        "summary": text.strip()[:200],
        "recommendations": [],
        "risk_notes": ["JSON 解析失败，请查看 raw_thinking"],
        "raw_thinking": None,
    }
