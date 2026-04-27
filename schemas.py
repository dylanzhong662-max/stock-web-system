from pydantic import BaseModel
from typing import Optional, List


class PositionCreate(BaseModel):
    asset: str
    ticker: str
    direction: str
    entry_price: float
    entry_date: str
    quantity: float
    cost_basis_usd: float
    stop_loss: Optional[float] = None
    profit_target: Optional[float] = None
    trailing_stop: bool = False
    source_signal: Optional[str] = None
    exchange: Optional[str] = None
    symbol: Optional[str] = None
    notes: Optional[str] = None


class PositionUpdate(BaseModel):
    stop_loss: Optional[float] = None
    profit_target: Optional[float] = None
    trailing_stop: Optional[bool] = None
    notes: Optional[str] = None
    quantity: Optional[float] = None


class PositionClose(BaseModel):
    exit_price: float
    exit_date: str
    exit_reason: str = "manual"
    notes: Optional[str] = None


class PositionResponse(BaseModel):
    id: int
    asset: str
    ticker: str
    direction: str
    entry_price: float
    entry_date: str
    quantity: float
    cost_basis_usd: float
    stop_loss: Optional[float]
    profit_target: Optional[float]
    trailing_stop: bool
    source_signal: Optional[str]
    exchange: Optional[str]
    symbol: Optional[str]
    notes: Optional[str]
    status: str
    created_at: str
    updated_at: str
    current_price: Optional[float] = None
    unrealized_pnl_usd: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    distance_to_stop_pct: Optional[float] = None
    distance_to_target_pct: Optional[float] = None
    position_status: Optional[str] = None
    latest_signal_action: Optional[str] = None
    latest_signal_bias: Optional[float] = None

    model_config = {"from_attributes": True}


class TradeCreate(BaseModel):
    asset: str
    ticker: str
    direction: str
    entry_price: float
    entry_date: str
    exit_price: float
    exit_date: str
    quantity: float
    cost_basis_usd: float
    exit_reason: Optional[str] = "manual"
    notes: Optional[str] = None


class TradeResponse(BaseModel):
    id: int
    position_id: Optional[int]
    asset: str
    ticker: str
    direction: str
    entry_price: float
    entry_date: str
    exit_price: float
    exit_date: str
    quantity: float
    cost_basis_usd: float
    realized_pnl_usd: Optional[float]
    realized_pnl_pct: Optional[float]
    exit_reason: Optional[str]
    holding_days: Optional[int]
    notes: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


class TradeStats(BaseModel):
    total: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    total_realized_pnl: float


class OrderResponse(BaseModel):
    id: int
    position_id: Optional[int]
    asset: str
    action: str
    side: str
    quantity: float
    order_type: str
    price: Optional[float]
    status: str
    note: Optional[str]
    generated_at: str
    executed_at: Optional[str]

    model_config = {"from_attributes": True}


class SignalSummary(BaseModel):
    asset: str
    action: Optional[str]
    bias_score: Optional[float]
    regime: Optional[str]
    entry_zone: Optional[str]
    stop_loss: Optional[float]
    profit_target: Optional[float]
    risk_reward_ratio: Optional[float]
    market_sentiment: Optional[str]
    analysis_date: Optional[str]


class Alert(BaseModel):
    asset: str
    status: str
    message: str
    severity: str


class DashboardSummary(BaseModel):
    portfolio_value: float
    total_unrealized_pnl_usd: float
    total_unrealized_pnl_pct: float
    active_signals: int
    total_assets: int = 12
    needs_action: int
    open_positions: int
    alerts: List[Alert]


class MacroData(BaseModel):
    sentiment: str
    vix: Optional[float] = None
    dxy: Optional[float] = None
    ten_year: Optional[float] = None
    btc_fear_greed: Optional[int] = None
    btc_fear_greed_label: Optional[str] = None
    last_scan_date: Optional[str] = None
    sector_ranking: list = []
    top_opportunities: list = []


# ── 截图解析 & 调仓建议 ────────────────────────────────────────────────────────

class ParsedPosition(BaseModel):
    asset: Optional[str] = None
    ticker: Optional[str] = None
    direction: Optional[str] = None
    quantity: Optional[float] = None
    entry_price: Optional[float] = None
    current_price: Optional[float] = None
    cost_basis: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    raw_text: Optional[str] = None


class ScreenshotParseResponse(BaseModel):
    success: bool
    positions: List[ParsedPosition]
    raw_ocr: Optional[str] = None
    error: Optional[str] = None


class AdviceRequest(BaseModel):
    positions: List[ParsedPosition]
    include_signals: bool = True
    model: Optional[str] = None  # None = 使用默认 deepseek-reasoner


class AdviceResponse(BaseModel):
    summary: str
    recommendations: List[dict]
    new_opportunities: List[dict] = []
    risk_notes: List[str]
    raw_thinking: Optional[str] = None
