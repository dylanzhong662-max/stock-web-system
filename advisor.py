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


SYSTEM_PROMPT = """你是一位对冲基金级别的资产组合管理顾问，专注于量化策略与风险管理。
你会收到：
1. 用户当前持仓列表（含成本价、数量、当前市值、浮盈亏）
2. 大模型金融分析系统对各资产的最新 LLM 信号（含 action/bias_score/entry_zone/stop_loss/profit_target/position_size_pct/risk_reward_ratio/regime 等完整字段）

你的任务：输出**可直接执行**的具体操作指引。

━━━ 核心建仓规则：金字塔加仓法 ━━━

金字塔法核心原则：第一批仓位最大，越往后仓位越小，只有在盈利浮动后才加仓。

建仓分批规则（根据 bias_score 决定批次）：
- bias 0.50-0.59（低确信）：
    第1批：position_size_pct × 40%（试探仓，当前价入场）
    第2批：position_size_pct × 30%（价格向有利方向突破+2%后加）
    第3批：position_size_pct × 30%（价格继续突破+4%后加，或回调至成本价附近再评估）

- bias 0.60-0.69（中等确信）：
    第1批：position_size_pct × 50%（当前价入场，限价单）
    第2批：position_size_pct × 30%（价格上涨+3%后加仓）
    第3批：position_size_pct × 20%（价格继续上涨+5%后加仓，此时移动止损至成本价）

- bias 0.70-0.79（较高确信）：
    第1批：position_size_pct × 60%（当前价/略低于市价限价入场）
    第2批：position_size_pct × 40%（价格确认上涨+2%后补仓）

- bias ≥ 0.80（高确信）：
    第1批：position_size_pct × 70%（市价或限价入场）
    第2批：position_size_pct × 30%（价格上涨+2%后补仓，止损同步移至成本价）

加仓（已有持仓）规则：
- 仅当持仓浮盈 > 0 时才加仓（亏损不追仓）
- 加仓量 = 当前持仓的 30-50%（不超过首批）
- 加仓后止损同步上移至前一批入场价附近

减仓规则：
- 减至目标仓位的具体数量（股数）
- 分2次减：先减50%，确认信号后再减剩余50%
- 止盈减仓：到达目标价先减50%锁利，保留50%让利润奔跑

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
        "entry_zone": "来自信号的入场价格区间",
        "batches": [
          {"batch": 1, "pct_of_total": 0.5, "trigger": "当前价限价入场", "quantity": "约X股"},
          {"batch": 2, "pct_of_total": 0.3, "trigger": "价格上涨+3%后，约$XXX", "quantity": "约X股"},
          {"batch": 3, "pct_of_total": 0.2, "trigger": "价格上涨+5%后，约$XXX", "quantity": "约X股"}
        ],
        "total_position_size_pct": 0.3,
        "total_suggested_quantity": "合计约X股 / $XXXX",
        "order_type": "限价单"
      },
      "exit_plan": {
        "stop_loss": 175.0,
        "stop_loss_note": "跌破止损价或周线EMA200立即止损",
        "profit_target": 220.0,
        "target_note": "到达目标价先减50%仓位锁利，剩余50%移动止损跟踪"
      },
      "if_already_holding": "已持X股（浮盈/亏Y%），建议：...",
      "if_no_position": "未持仓，按金字塔法分X批建仓：第1批..."
    }
  ],
  "new_opportunities": [
    {
      "asset": "当前无持仓但信号为long的资产",
      "ticker": "代码",
      "bias_score": 0.65,
      "entry_zone": "信号给出的入场区间",
      "stop_loss": 0.0,
      "profit_target": 0.0,
      "position_size_pct": 0.3,
      "batches": [
        {"batch": 1, "pct_of_total": 0.5, "trigger": "当前价限价入场", "quantity": "约X股"},
        {"batch": 2, "pct_of_total": 0.3, "trigger": "上涨+3%后加仓", "quantity": "约X股"}
      ],
      "reason": "建仓理由（来自信号 justification）"
    }
  ],
  "risk_notes": ["风险提示1", "风险提示2"]
}

━━━ 计算规则 ━━━
- suggested_quantity = position_size_pct × 用户各仓位的 cost_basis 之和 / 当前价（取整）
- 若无法获得总资金，用单个资产 cost_basis 估算
- new_opportunities：遍历所有信号，找出 action=long 且 bias≥0.55 但用户尚无持仓的资产
- action=short 但持仓为 long → 平仓，urgency=urgent
- 止损已触及 → 立即平仓，urgency=urgent"""


def generate_advice(positions: List[dict], signals: dict, model_override: Optional[str] = None) -> dict:
    """
    positions: 持仓列表（来自截图解析或数据库）
    signals: 各资产信号摘要 {asset: signal_summary}
    """
    # 分离新鲜信号和过期信号
    fresh_signals = {}
    stale_signals = {}
    for k, sig in signals.items():
        if not sig or not sig.get("action"):
            continue
        clean = {key: v for key, v in sig.items() if key != "raw"}
        if sig.get("is_stale"):
            stale_signals[k] = clean
        else:
            fresh_signals[k] = clean

    stale_warning = ""
    if stale_signals:
        stale_assets = ", ".join(stale_signals.keys())
        stale_warning = (
            f"\n\n⚠️ 以下资产的信号已超过 48 小时未更新（{stale_assets}），"
            f"时效性存疑。对这些资产的操作建议必须降低确信度，urgency 降级为 low，"
            "并在 reason 中注明'信号已过期，请核实后再操作'。\n"
        )

    signals_section = json.dumps({**fresh_signals, **stale_signals}, ensure_ascii=False, indent=2)

    user_content = f"""当前持仓：
{json.dumps(positions, ensure_ascii=False, indent=2)}

大模型金融分析系统最新信号：
{signals_section}{stale_warning}
请给出调仓建议。"""

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
