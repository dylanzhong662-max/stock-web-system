"""
多客户端 LLM 配置：
  - DeepSeek 模型（deepseek-*）→ 原生 API https://api.deepseek.com，使用 DEEPSEEK_API_KEY
  - Qwen 模型（qwen-*/qwq-*）→ DashScope https://dashscope.aliyuncs.com/compatible-mode/v1，使用 QWEN_API_KEY
  - GLM 模型（glm-*）→ 智谱 API https://open.bigmodel.cn/api/paas/v4/，使用 ZHIPU_API_KEY
  - Claude 模型（claude-*）→ CloseAI Anthropic 原生协议 https://api.openai-proxy.org/anthropic，使用 LLM_API_KEY
  - 其他模型（Gemini / GPT / o3）→ CloseAI OpenAI 兼容协议，使用 LLM_API_KEY

环境变量：
  DEEPSEEK_API_KEY  — DeepSeek 原生 API Key
  QWEN_API_KEY      — 通义千问 DashScope API Key
  ZHIPU_API_KEY     — 智谱 GLM API Key
  LLM_API_KEY       — CloseAI 代理 API Key（Claude / Gemini / GPT / o3 等模型）
  LLM_BASE_URL      — CloseAI OpenAI 兼容代理地址，默认 https://api.openai-proxy.org/v1
  LLM_MODEL         — 默认模型，默认 deepseek-reasoner
"""
import os
from typing import AsyncGenerator

from openai import AsyncOpenAI

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_QWEN_BASE_URL     = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_ZHIPU_BASE_URL    = "https://open.bigmodel.cn/api/paas/v4/"
_CLOSEAI_BASE_URL  = "https://api.openai-proxy.org/v1"
_CLOSEAI_ANTHROPIC_BASE_URL = "https://api.openai-proxy.org/anthropic"
DEFAULT_MODEL      = "deepseek-reasoner"

# Claude 模型 → CloseAI Anthropic 协议 model id 映射
_CLAUDE_MODEL_MAP = {
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-sonnet-4-6": "claude-sonnet-4-20250514",
}

# 千问模型默认启用 thinking；以下场景显式关闭以节省 token/加速
_QWEN_NO_THINKING_MODELS = {"qwen3.6-flash", "qwen3.7-plus"}

# Claude 推理模型：启用 extended thinking
_CLAUDE_THINKING_MODELS = {"claude-sonnet-5"}

AVAILABLE_MODELS = [
    {"id": "gemini-3.1-pro-preview",  "name": "Gemini 3.1 Pro",   "tag": ""},
    {"id": "gemini-3-pro-preview",    "name": "Gemini 3 Pro",     "tag": ""},
    {"id": "gemini-2.5-pro",          "name": "Gemini 2.5 Pro",   "tag": ""},
    {"id": "deepseek-v4-pro",         "name": "DeepSeek V4 Pro",  "tag": "快速"},
    {"id": "deepseek-reasoner",       "name": "DeepSeek R1",      "tag": "默认"},
    {"id": "glm-5.2",                 "name": "GLM 5.2",          "tag": "智谱"},
    {"id": "qwen3.7-max",             "name": "Qwen3.7 Max",      "tag": "推理"},
    {"id": "qwen3.7-plus",            "name": "Qwen3.7 Plus",     "tag": "均衡"},
    {"id": "qwen3.6-flash",           "name": "Qwen3.6 Flash",    "tag": "快速"},
    {"id": "qwq-plus",                "name": "QwQ Plus",         "tag": "推理"},
    {"id": "claude-sonnet-5",         "name": "Claude Sonnet 5",  "tag": "推理"},
    {"id": "claude-sonnet-4-6",       "name": "Claude Sonnet 4.6","tag": ""},
    {"id": "gpt-4.1",                 "name": "GPT-4.1",          "tag": ""},
    {"id": "gpt-4o",                  "name": "GPT-4o",           "tag": ""},
    {"id": "o3",                      "name": "o3",               "tag": ""},
]

_deepseek_client: AsyncOpenAI | None = None
_qwen_client:     AsyncOpenAI | None = None
_zhipu_client:    AsyncOpenAI | None = None
_proxy_client:    AsyncOpenAI | None = None
_anthropic_client = None  # anthropic.AsyncAnthropic


def is_deepseek(model: str) -> bool:
    return model.startswith("deepseek")


def is_qwen(model: str) -> bool:
    return model.startswith("qwen") or model.startswith("qwq")


def is_claude(model: str) -> bool:
    return model.startswith("claude")


def is_glm(model: str) -> bool:
    return model.startswith("glm")


def has_reasoning(model: str) -> bool:
    """模型是否会产生 reasoning_content（需要在流式中跳过）"""
    if model == "deepseek-reasoner":
        return True
    if is_qwen(model):
        return True
    if model in _CLAUDE_THINKING_MODELS:
        return True
    return False


def get_client(model: str = "") -> AsyncOpenAI:
    """根据模型名返回对应的 OpenAI 兼容客户端（非 Claude 模型使用）"""
    global _deepseek_client, _qwen_client, _zhipu_client, _proxy_client

    if is_deepseek(model):
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if _deepseek_client is None or _deepseek_client.api_key != api_key:
            _deepseek_client = AsyncOpenAI(api_key=api_key, base_url=_DEEPSEEK_BASE_URL)
        return _deepseek_client
    elif is_qwen(model):
        api_key = os.environ.get("QWEN_API_KEY", "")
        if _qwen_client is None or _qwen_client.api_key != api_key:
            _qwen_client = AsyncOpenAI(api_key=api_key, base_url=_QWEN_BASE_URL)
        return _qwen_client
    elif is_glm(model):
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        if _zhipu_client is None or _zhipu_client.api_key != api_key:
            _zhipu_client = AsyncOpenAI(api_key=api_key, base_url=_ZHIPU_BASE_URL)
        return _zhipu_client
    else:
        api_key  = os.environ.get("LLM_API_KEY", "")
        base_url = os.environ.get("LLM_BASE_URL", _CLOSEAI_BASE_URL)
        if _proxy_client is None or _proxy_client.api_key != api_key:
            _proxy_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return _proxy_client


def _get_anthropic_client():
    """返回 Anthropic 原生 AsyncClient（Claude 专用）"""
    global _anthropic_client
    from anthropic import AsyncAnthropic
    api_key = os.environ.get("LLM_API_KEY", "")
    if _anthropic_client is None or _anthropic_client.api_key != api_key:
        _anthropic_client = AsyncAnthropic(
            api_key=api_key,
            base_url=_CLOSEAI_ANTHROPIC_BASE_URL,
        )
    return _anthropic_client


def no_temperature(model: str) -> bool:
    """推理模型不支持 temperature 参数"""
    if model == "deepseek-reasoner":
        return True
    if model in _CLAUDE_THINKING_MODELS:
        return True
    return False


def temp_kwargs(model: str, temperature: float = 0.3) -> dict:
    """返回 temperature kwarg，推理模型自动跳过"""
    if no_temperature(model):
        return {}
    return {"temperature": temperature}


def build_extra_params(model: str) -> dict:
    """构建模型特有的额外参数（如千问 thinking 模式开关）"""
    if is_qwen(model):
        if model in _QWEN_NO_THINKING_MODELS:
            return {"extra_body": {"enable_thinking": False}}
        return {"extra_body": {"enable_thinking": True}}
    return {}


async def stream_chat(
    model: str,
    messages: list[dict],
    max_tokens: int = 8000,
    temperature: float = 0.3,
    **extra,
) -> AsyncGenerator[str, None]:
    """
    统一流式接口：根据模型自动选择协议（Claude → Anthropic 原生，其他 → OpenAI 兼容）。
    只 yield 正文 text，自动跳过 thinking/reasoning 内容。
    """
    if is_claude(model):
        async for text in _stream_claude(model, messages, max_tokens, temperature):
            yield text
    else:
        kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
            **temp_kwargs(model, temperature),
            **build_extra_params(model),
            **extra,
        )
        stream = await get_client(model).chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if has_reasoning(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                yield delta.content


async def _stream_claude(
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[str, None]:
    """通过 Anthropic 原生协议流式调用 Claude，支持 extended thinking"""
    client = _get_anthropic_client()
    api_model = _CLAUDE_MODEL_MAP.get(model, model)

    # 分离 system message
    system_text = None
    user_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            user_messages.append(msg)

    kwargs = dict(
        model=api_model,
        messages=user_messages,
        max_tokens=max_tokens,
    )
    if system_text:
        kwargs["system"] = system_text

    if model in _CLAUDE_THINKING_MODELS:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["temperature"] = 1
    else:
        kwargs["temperature"] = temperature

    async with client.messages.stream(**kwargs) as stream:
        async for event in stream:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    yield event.delta.text


async def complete_chat(
    model: str,
    messages: list[dict],
    max_tokens: int = 2000,
    temperature: float = 0,
) -> str:
    """非流式调用，返回完整文本。Claude 走原生协议，其他走 OpenAI 兼容。"""
    if is_claude(model):
        return await _complete_claude(model, messages, max_tokens, temperature)
    else:
        kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            **temp_kwargs(model, temperature),
            **build_extra_params(model),
        )
        resp = await get_client(model).chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


async def _complete_claude(
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
) -> str:
    """Anthropic 原生非流式调用"""
    client = _get_anthropic_client()
    api_model = _CLAUDE_MODEL_MAP.get(model, model)

    system_text = None
    user_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            user_messages.append(msg)

    kwargs = dict(
        model=api_model,
        messages=user_messages,
        max_tokens=max_tokens,
    )
    if system_text:
        kwargs["system"] = system_text

    if model in _CLAUDE_THINKING_MODELS:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["temperature"] = 1
    else:
        kwargs["temperature"] = temperature

    resp = await client.messages.create(**kwargs)
    parts = []
    for block in resp.content:
        if block.type == "text":
            parts.append(block.text)
    return "".join(parts)


def resolve_model(model: str | None) -> str:
    if model:
        return model
    return os.environ.get("LLM_MODEL", DEFAULT_MODEL)
