from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, Index

from database import Base


class ReportCache(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(20), nullable=False)   # chain | company | portfolio
    cache_key = Column(String(300), nullable=False)    # topic::model 组合键
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_report_type_key", "report_type", "cache_key", unique=True),
    )


class Prediction(Base):
    """LLM 报告中的方向性判断，用于后续计算命中率 / IC"""
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(20), nullable=False)       # chain | company | portfolio
    cache_key = Column(String(200), nullable=False)        # 行业名 / ticker / "latest"
    ticker = Column(String(32), nullable=True, index=True) # 行业级预测可为空
    direction = Column(String(16), nullable=False)         # bullish | bearish | neutral
    confidence = Column(Float, nullable=True)              # 0.0 - 1.0
    horizon_days = Column(Integer, nullable=False)         # 预测时效
    target_price = Column(Float, nullable=True)
    entry_price = Column(Float, nullable=True)             # 预测时价格，用于后续计算收益
    rationale = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolve_at = Column(DateTime, nullable=False, index=True)  # created_at + horizon_days

    resolved_at = Column(DateTime, nullable=True)
    resolved_price = Column(Float, nullable=True)
    realized_return = Column(Float, nullable=True)         # (resolved - entry) / entry
    benchmark_return = Column(Float, nullable=True)        # 同期 SPY 收益（chain/company）
    excess_return = Column(Float, nullable=True)           # 相对基准超额
    hit = Column(Boolean, nullable=True)                   # direction 方向是否正确


Index("ix_predictions_resolve_status", Prediction.resolve_at, Prediction.resolved_at)


class WatchItem(Base):
    """监控清单项——从持仓分析报告中提取的关键变量"""
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String(32), nullable=True, index=True)   # 关联持仓；None=宏观变量
    variable = Column(String(200), nullable=False)            # 监控变量名
    frequency = Column(String(20), nullable=False)            # daily/weekly/monthly/quarterly
    bullish_signal = Column(Text, nullable=True)              # 看多触发条件
    bearish_signal = Column(Text, nullable=True)              # 看空触发条件
    current_value = Column(String(100), nullable=True)        # 最近一次观测值
    last_checked = Column(DateTime, nullable=True)
    source = Column(String(50), default="portfolio_report")   # 来源
    priority = Column(Integer, default=2)                     # 1=高 2=中 3=低
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, nullable=True)                       # 用户自定义备注
