"""
双客户端 LLM 配置：
  - DeepSeek 模型（deepseek-*）→ 原生 API https://api.deepseek.com，使用 DEEPSEEK_API_KEY
  - 其他模型（Gemini / GPT / Claude）→ CloseAI 代理，使用 LLM_API_KEY

环境变量：
  DEEPSEEK_API_KEY  — DeepSeek 原生 API Key
  LLM_API_KEY       — CloseAI 代理 API Key（非 DeepSeek 模型）
  LLM_BASE_URL      — CloseAI 代理地址，默认 https://api.openai-proxy.org/v1
  LLM_MODEL         — 默认模型，默认 deepseek-reasoner
"""
import os
from openai import AsyncOpenAI

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_CLOSEAI_BASE_URL  = "https://api.openai-proxy.org/v1"
DEFAULT_MODEL      = "deepseek-reasoner"

AVAILABLE_MODELS = [
    {"id": "gemini-3.1-pro-preview",  "name": "Gemini 3.1 Pro",   "tag": ""},
    {"id": "gemini-3-pro-preview",    "name": "Gemini 3 Pro",     "tag": ""},
    {"id": "gemini-2.5-pro",          "name": "Gemini 2.5 Pro",   "tag": ""},
    {"id": "deepseek-v4-pro",         "name": "DeepSeek V4 Pro",  "tag": "快速"},
    {"id": "deepseek-reasoner",       "name": "DeepSeek R1",      "tag": "默认"},
    {"id": "claude-sonnet-4-6",       "name": "Claude Sonnet 4.6","tag": ""},
    {"id": "gpt-4.1",                 "name": "GPT-4.1",          "tag": ""},
    {"id": "gpt-4o",                  "name": "GPT-4o",           "tag": ""},
    {"id": "o3",                      "name": "o3",               "tag": ""},
]

_deepseek_client: AsyncOpenAI | None = None
_proxy_client:    AsyncOpenAI | None = None


def is_deepseek(model: str) -> bool:
    return model.startswith("deepseek")


def get_client(model: str = "") -> AsyncOpenAI:
    """根据模型名返回对应客户端，DeepSeek 走原生 API，其余走代理"""
    global _deepseek_client, _proxy_client

    if is_deepseek(model):
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if _deepseek_client is None or _deepseek_client.api_key != api_key:
            _deepseek_client = AsyncOpenAI(api_key=api_key, base_url=_DEEPSEEK_BASE_URL)
        return _deepseek_client
    else:
        api_key  = os.environ.get("LLM_API_KEY", "")
        base_url = os.environ.get("LLM_BASE_URL", _CLOSEAI_BASE_URL)
        if _proxy_client is None or _proxy_client.api_key != api_key:
            _proxy_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return _proxy_client


def resolve_model(model: str | None) -> str:
    if model:
        return model
    return os.environ.get("LLM_MODEL", DEFAULT_MODEL)
