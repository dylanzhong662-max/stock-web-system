import os
import re
from datetime import datetime, timedelta

from database import SessionLocal
from models import ReportCache

TTL = {
    "chain": 24,
    "company": 6,
    "portfolio": 1,
}


def get_cached(report_type: str, key: str) -> str | None:
    db = SessionLocal()
    try:
        row = (
            db.query(ReportCache)
            .filter(
                ReportCache.report_type == report_type,
                ReportCache.cache_key == key,
                ReportCache.expires_at > datetime.utcnow(),
            )
            .first()
        )
        return row.content if row else None
    finally:
        db.close()


def save_cache(report_type: str, key: str, content: str):
    hours = TTL.get(report_type, 6)
    db = SessionLocal()
    try:
        existing = (
            db.query(ReportCache)
            .filter(ReportCache.report_type == report_type, ReportCache.cache_key == key)
            .first()
        )
        if existing:
            existing.content = content
            existing.created_at = datetime.utcnow()
            existing.expires_at = datetime.utcnow() + timedelta(hours=hours)
        else:
            db.add(ReportCache(
                report_type=report_type,
                cache_key=key,
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
