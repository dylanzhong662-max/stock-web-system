import os
import re
from datetime import datetime, timedelta

from database import SessionLocal
from models import ReportCache

TTL = {
    "chain": 24,      # 产业链：日级别稳定，每天刷新一次
    "company": 6,     # 公司分析：半天刷新，覆盖盘中重要新闻
    "portfolio": 1,   # 持仓研究：持仓可随时变，1h 是合理下限
    "technical": 4,   # 技术分析：日线级别，4h 刷新
    "qa": 4,          # 通用问答：无日内实时性，4h 避免重复打
}


def _composite_key(key: str, model: str | None) -> str:
    """同一主题 + 同一模型才命中缓存，不同模型重新生成"""
    if model:
        return f"{key}::{model}"
    return key


def get_cached(report_type: str, key: str, model: str | None = None) -> str | None:
    composite = _composite_key(key, model)
    db = SessionLocal()
    try:
        row = (
            db.query(ReportCache)
            .filter(
                ReportCache.report_type == report_type,
                ReportCache.cache_key == composite,
                ReportCache.expires_at > datetime.utcnow(),
            )
            .first()
        )
        return row.content if row else None
    finally:
        db.close()


def save_cache(report_type: str, key: str, content: str, model: str | None = None):
    composite = _composite_key(key, model)
    hours = TTL.get(report_type, 6)
    db = SessionLocal()
    try:
        existing = (
            db.query(ReportCache)
            .filter(ReportCache.report_type == report_type, ReportCache.cache_key == composite)
            .first()
        )
        if existing:
            existing.content = content
            existing.created_at = datetime.utcnow()
            existing.expires_at = datetime.utcnow() + timedelta(hours=hours)
        else:
            db.add(ReportCache(
                report_type=report_type,
                cache_key=composite,
                content=content,
                expires_at=datetime.utcnow() + timedelta(hours=hours),
            ))
        db.commit()
    finally:
        db.close()


def format_report(content: str, source_note: str = "") -> str:
    """规范化 LLM 输出的 Markdown，添加时间戳和数据来源"""
    # 去掉 <think> 标签（DeepSeek R1 推理过程）
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    footer_parts = [f"*生成时间：{timestamp}*"]
    if source_note:
        footer_parts.append(f"*数据来源：{source_note}*")

    return content + "\n\n---\n" + " | ".join(footer_parts)
