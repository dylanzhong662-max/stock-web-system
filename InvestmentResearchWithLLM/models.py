from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime

from database import Base


class ReportCache(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(20), nullable=False)   # chain | company | portfolio
    cache_key = Column(String(200), nullable=False, unique=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
