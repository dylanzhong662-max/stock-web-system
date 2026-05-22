"""LLM 输出 JSON 解析工具 — 统一处理 <think> 标签、markdown 代码块、大括号/方括号匹配"""
import json
import re
from typing import Optional


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)


def _extract_by_brackets(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start: i + 1]
    return None


def parse_json_object(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON 对象，支持直接 JSON / markdown 代码块 / 大括号匹配"""
    text = _strip_think(text)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass

    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if code_block:
        candidate = code_block.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass
        raw = _extract_by_brackets(candidate, "{", "}")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass

    raw = _extract_by_brackets(text, "{", "}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


def parse_json_array(text: str) -> list[dict]:
    """从 LLM 输出中提取 JSON 数组"""
    text = _strip_think(text)
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            return json.loads(stripped)
        except Exception:
            pass

    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if code_block:
        candidate = code_block.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass

    raw = _extract_by_brackets(text, "[", "]")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []
