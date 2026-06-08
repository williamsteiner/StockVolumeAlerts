"""Stock and ETF unusual volume alert monitor.

This module provides live monitoring, one-shot scans, backtesting, threshold
optimization, email/SMS notifications, report generation, and chart output.
All runtime behavior is configured through ``config.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import logging.handlers
import smtplib
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dt_time, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import holidays
import pandas as pd
import yfinance as yf

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:
    plt = None


APP_NAME = "StockVolumeAlerts"
CONFIG_PATH = Path("config.json")
HISTORY_PATH = Path("alert_history.json")
LOG_PATH = Path("logs") / "volume_alert.log"
REPORTS_DIR = Path("reports")
CHARTS_DIR = REPORTS_DIR / "charts"
SUPPORTED_DIRECTIONS = {"up", "down", "both"}
SUPPORTED_PRICE_REFERENCES = {
    "previous_close",
    "20_day_moving_average",
    "50_day_moving_average",
}
SMS_GATEWAYS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
}
OPTIMIZE_VOLUME_THRESHOLDS = [50, 100, 150, 200]
OPTIMIZE_PRICE_THRESHOLDS = [1, 2, 3, 5, 10]
POSITION_NORMAL = "NORMAL"
POSITION_WAITING_FOR_REBUY = "WAITING_FOR_REBUY"
STRATEGY_SELL_HALF = "SELL_HALF"
STRATEGY_REBUY = "REBUY"


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


class DataError(RuntimeError):
    """Raised when market data cannot be evaluated."""


@dataclass(frozen=True)
class ScheduleConfig:
    """Runtime schedule configuration."""

    interval_minutes: int
    market_start: dt_time
    market_end: dt_time
    timezone: str


@dataclass(frozen=True)
class TextRecipient:
    """SMS recipient configured through a carrier email gateway."""

    number: str
    carrier: str


@dataclass(frozen=True)
class NotificationConfig:
    """Email and text recipient configuration."""

    emails: list[str]
    texts: list[TextRecipient]


@dataclass(frozen=True)
class SmtpConfig:
    """SMTP connection configuration."""

    server: str
    port: int
    username: str
    password: str


@dataclass(frozen=True)
class GeneralConfig:
    """General runtime behavior."""

    market_holidays: bool
    skip_weekends: bool
    log_level: str
    alert_cooldown_minutes: int


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest output configuration."""

    years: int
    generate_charts: bool
    generate_html_report: bool


@dataclass(frozen=True)
class PriceFilterConfig:
    """Optional price filter settings."""

    enabled: bool
    percent: float
    direction: str
    reference: str


@dataclass(frozen=True)
class SymbolConfig:
    """Per-symbol alert settings."""

    ticker: str
    enabled: bool
    average_days: int
    volume_percent: float
    direction: str
    repeat_alerts: bool
    minimum_volume: int
    price_filter: PriceFilterConfig


@dataclass(frozen=True)
class AppConfig:
    """Full application configuration."""

    schedule: ScheduleConfig
    notifications: NotificationConfig
    smtp: SmtpConfig
    general: GeneralConfig
    backtest: BacktestConfig
    symbols: list[SymbolConfig]


@dataclass(frozen=True)
class AlertMetrics:
    """Calculated alert metrics for one symbol on one date/time."""

    ticker: str
    triggered_at: datetime
    volume: int
    average_volume: float
    percent_change: float
    rvol: float
    open_price: float
    price: float
    candle_color: str
    candle_direction: str
    candle_change_percent: float
    price_change_percent: float | None
    price_filter_enabled: bool
    price_filter_result: bool
    volume_condition_result: bool
    final_alert_result: bool
    direction_triggered: str
    rsi14: float | None
    high_52_week: float | None
    distance_from_52_week_high_percent: float | None
    sma200: float | None
    distance_above_200ma_percent: float | None
    profit_taking_score: int
    position_state: str = POSITION_NORMAL
    strategy_alert_type: str = ""
    strategy_reasons: tuple[str, ...] = ()
    sell_price: float | None = None
    rebuy_reason: str = ""


@dataclass
class ScanResult:
    """Results collected during a live scan."""

    alerts: list[AlertMetrics] = field(default_factory=list)
    evaluated: int = 0
    errors: int = 0


@dataclass(frozen=True)
class BacktestResult:
    """Backtest rows and summary rows."""

    details: list[dict[str, Any]]
    summary: list[dict[str, Any]]
    cooldown: list[dict[str, Any]]
    strategy_pairs: list[dict[str, Any]]


def ensure_directories() -> None:
    """Create runtime directories if they do not exist."""

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)


def parse_clock(value: str, field_name: str) -> dt_time:
    """Parse HH:MM clock values from configuration."""

    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{field_name} must use HH:MM format") from exc


def require_mapping(data: Any, name: str) -> dict[str, Any]:
    """Return a mapping or raise a configuration error."""

    if not isinstance(data, dict):
        raise ConfigError(f"{name} must be an object")
    return data


def require_list(data: Any, name: str) -> list[Any]:
    """Return a list or raise a configuration error."""

    if not isinstance(data, list):
        raise ConfigError(f"{name} must be a list")
    return data


def clean_email(value: str) -> str:
    """Clean markdown mailto values that may appear in pasted examples."""

    text = str(value).strip()
    if text.startswith("[") and "](mailto:" in text and text.endswith(")"):
        return text.split("](mailto:", 1)[1][:-1]
    return text


def validate_direction(value: str, field_name: str) -> str:
    """Validate alert or price direction values."""

    direction = str(value).lower()
    if direction not in SUPPORTED_DIRECTIONS:
        raise ConfigError(f"{field_name} must be one of {sorted(SUPPORTED_DIRECTIONS)}")
    return direction


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load and validate application configuration from JSON."""

    if not path.exists():
        raise ConfigError(f"Missing {path}. Copy config.json.example to config.json.")

    with path.open("r", encoding="utf-8") as handle:
        raw = require_mapping(json.load(handle), "config")

    schedule_raw = require_mapping(raw.get("schedule"), "schedule")
    notifications_raw = require_mapping(raw.get("notifications"), "notifications")
    smtp_raw = require_mapping(raw.get("smtp"), "smtp")
    general_raw = require_mapping(raw.get("general"), "general")
    backtest_raw = require_mapping(raw.get("backtest"), "backtest")
    symbols_raw = require_list(raw.get("symbols"), "symbols")

    schedule = ScheduleConfig(
        interval_minutes=int(schedule_raw["interval_minutes"]),
        market_start=parse_clock(str(schedule_raw["market_start"]), "schedule.market_start"),
        market_end=parse_clock(str(schedule_raw["market_end"]), "schedule.market_end"),
        timezone=str(schedule_raw["timezone"]),
    )
    if schedule.interval_minutes <= 0:
        raise ConfigError("schedule.interval_minutes must be greater than zero")
    ZoneInfo(schedule.timezone)

    texts = []
    for item in require_list(notifications_raw.get("texts", []), "notifications.texts"):
        text_raw = require_mapping(item, "notifications.texts[]")
        carrier = str(text_raw["carrier"]).lower()
        if carrier not in SMS_GATEWAYS:
            raise ConfigError(f"Unsupported text carrier: {carrier}")
        texts.append(TextRecipient(number=str(text_raw["number"]), carrier=carrier))

    notifications = NotificationConfig(
        emails=[clean_email(email) for email in notifications_raw.get("emails", [])],
        texts=texts,
    )

    smtp = SmtpConfig(
        server=str(smtp_raw["server"]),
        port=int(smtp_raw["port"]),
        username=clean_email(str(smtp_raw["username"])),
        password=str(smtp_raw["password"]),
    )

    general = GeneralConfig(
        market_holidays=bool(general_raw["market_holidays"]),
        skip_weekends=bool(general_raw["skip_weekends"]),
        log_level=str(general_raw["log_level"]).upper(),
        alert_cooldown_minutes=int(general_raw["alert_cooldown_minutes"]),
    )
    if general.alert_cooldown_minutes < 0:
        raise ConfigError("general.alert_cooldown_minutes cannot be negative")

    backtest = BacktestConfig(
        years=int(backtest_raw["years"]),
        generate_charts=bool(backtest_raw["generate_charts"]),
        generate_html_report=bool(backtest_raw["generate_html_report"]),
    )
    if backtest.years <= 0:
        raise ConfigError("backtest.years must be greater than zero")

    symbols: list[SymbolConfig] = []
    for item in symbols_raw:
        symbol_raw = require_mapping(item, "symbols[]")
        price_raw = require_mapping(symbol_raw.get("price_filter", {}), "price_filter")
        reference = str(price_raw.get("reference", "previous_close")).lower()
        if reference not in SUPPORTED_PRICE_REFERENCES:
            raise ConfigError(f"Unsupported price reference: {reference}")
        price_filter = PriceFilterConfig(
            enabled=bool(price_raw.get("enabled", False)),
            percent=float(price_raw.get("percent", 0)),
            direction=validate_direction(
                price_raw.get("direction", "up"),
                "price_filter.direction",
            ),
            reference=reference,
        )
        symbol = SymbolConfig(
            ticker=str(symbol_raw["ticker"]).upper(),
            enabled=bool(symbol_raw.get("enabled", True)),
            average_days=int(symbol_raw["average_days"]),
            volume_percent=float(symbol_raw["volume_percent"]),
            direction=validate_direction(symbol_raw["direction"], "symbol.direction"),
            repeat_alerts=bool(symbol_raw.get("repeat_alerts", False)),
            minimum_volume=int(symbol_raw.get("minimum_volume", 0)),
            price_filter=price_filter,
        )
        if symbol.average_days <= 0:
            raise ConfigError(f"{symbol.ticker}: average_days must be greater than zero")
        if symbol.volume_percent < 0:
            raise ConfigError(f"{symbol.ticker}: volume_percent cannot be negative")
        symbols.append(symbol)

    if not symbols:
        raise ConfigError("At least one symbol is required")

    return AppConfig(
        schedule=schedule,
        notifications=notifications,
        smtp=smtp,
        general=general,
        backtest=backtest,
        symbols=symbols,
    )


def setup_logging(level_name: str = "INFO") -> logging.Logger:
    """Configure rotating file and console logging."""

    ensure_directories()
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


class MarketCalendar:
    """Market-hours helper using configured timezone and US holidays."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.timezone = ZoneInfo(config.schedule.timezone)
        if hasattr(holidays, "country_holidays"):
            self.us_holidays = holidays.country_holidays("US")
        elif hasattr(holidays, "US"):
            self.us_holidays = holidays.US()
        else:
            self.us_holidays = set()

    def now(self) -> datetime:
        """Return current time in configured timezone."""

        return datetime.now(self.timezone)

    def is_market_day(self, day: date) -> bool:
        """Return whether scans may run for the given date."""

        if self.config.general.skip_weekends and day.weekday() >= 5:
            return False
        if self.config.general.market_holidays and day in self.us_holidays:
            return False
        return True

    def is_market_open(self, moment: datetime) -> bool:
        """Return whether the configured market window is open."""

        if not self.is_market_day(moment.date()):
            return False
        current = moment.time()
        return self.config.schedule.market_start <= current <= self.config.schedule.market_end

    def market_end_today(self, moment: datetime) -> datetime:
        """Return market close datetime for a day."""

        return datetime.combine(
            moment.date(),
            self.config.schedule.market_end,
            tzinfo=self.timezone,
        )


class AlertHistory:
    """Persistent alert history and in-run cooldown state."""

    def __init__(self, path: Path = HISTORY_PATH) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self.last_alert_by_ticker: dict[str, datetime] = {}
        self.strategy_state_by_ticker: dict[str, tuple[str, float | None]] = {}
        self.load()

    def load(self) -> None:
        """Load alert history if present."""

        if not self.path.exists():
            self.records = []
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.records = data if isinstance(data, list) else []
            for record in self.records:
                timestamp = record.get("timestamp")
                ticker = record.get("ticker")
                if isinstance(timestamp, str) and isinstance(ticker, str):
                    parsed = datetime.fromisoformat(timestamp)
                    previous = self.last_alert_by_ticker.get(ticker)
                    if previous is None or parsed > previous:
                        self.last_alert_by_ticker[ticker] = parsed
                strategy_alert = record.get("strategy_alert_type")
                if isinstance(ticker, str) and strategy_alert == STRATEGY_SELL_HALF:
                    self.strategy_state_by_ticker[ticker] = (
                        POSITION_WAITING_FOR_REBUY,
                        float(record.get("sell_price") or record.get("price")),
                    )
                elif isinstance(ticker, str) and strategy_alert == STRATEGY_REBUY:
                    self.strategy_state_by_ticker[ticker] = (POSITION_NORMAL, None)
        except (json.JSONDecodeError, OSError, ValueError):
            self.records = []

    def save(self) -> None:
        """Persist alert history to disk."""

        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.records, handle, indent=2)

    def has_duplicate(self, ticker: str, day: date, direction: str) -> bool:
        """Return whether an alert already exists for date/ticker/direction."""

        day_text = day.isoformat()
        return any(
            record.get("date") == day_text
            and record.get("ticker") == ticker
            and record.get("direction") == direction
            for record in self.records
        )

    def is_in_cooldown(
        self,
        ticker: str,
        moment: datetime,
        cooldown_minutes: int,
    ) -> bool:
        """Return whether ticker is still inside its cooldown window."""

        previous = self.last_alert_by_ticker.get(ticker)
        if previous is None or cooldown_minutes <= 0:
            return False
        return moment - previous < timedelta(minutes=cooldown_minutes)

    def record(self, metrics: AlertMetrics) -> None:
        """Record an alert in memory and on disk."""

        self.records.append(
            {
                "date": metrics.triggered_at.date().isoformat(),
                "timestamp": metrics.triggered_at.isoformat(),
                "ticker": metrics.ticker,
                "direction": metrics.direction_triggered,
                "volume": metrics.volume,
                "average_volume": metrics.average_volume,
                "percent_change": metrics.percent_change,
                "rvol": metrics.rvol,
                "open_price": metrics.open_price,
                "price": metrics.price,
                "candle_color": metrics.candle_color,
                "candle_direction": metrics.candle_direction,
                "candle_change_percent": metrics.candle_change_percent,
                "price_change_percent": metrics.price_change_percent,
                "rsi14": metrics.rsi14,
                "high_52_week": metrics.high_52_week,
                "distance_from_52_week_high_percent": (
                    metrics.distance_from_52_week_high_percent
                ),
                "sma200": metrics.sma200,
                "distance_above_200ma_percent": metrics.distance_above_200ma_percent,
                "profit_taking_score": metrics.profit_taking_score,
                "position_state": metrics.position_state,
                "strategy_alert_type": metrics.strategy_alert_type,
                "strategy_reasons": list(metrics.strategy_reasons),
                "sell_price": metrics.sell_price,
                "rebuy_reason": metrics.rebuy_reason,
            }
        )
        self.last_alert_by_ticker[metrics.ticker] = metrics.triggered_at
        if metrics.strategy_alert_type == STRATEGY_SELL_HALF:
            self.strategy_state_by_ticker[metrics.ticker] = (
                POSITION_WAITING_FOR_REBUY,
                metrics.sell_price,
            )
        elif metrics.strategy_alert_type == STRATEGY_REBUY:
            self.strategy_state_by_ticker[metrics.ticker] = (POSITION_NORMAL, None)
        self.save()

    def strategy_state(self, ticker: str) -> tuple[str, float | None]:
        """Return the latest persisted strategy state for a ticker."""

        return self.strategy_state_by_ticker.get(ticker, (POSITION_NORMAL, None))


class MarketDataProvider:
    """Fetches market data from yfinance."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def fetch_live(self, symbol: SymbolConfig) -> pd.DataFrame:
        """Fetch recent history for a live symbol evaluation."""

        lookback_days = max(symbol.average_days + 15, 420)
        period = f"{lookback_days}d"
        self.logger.info("Downloading live data for %s", symbol.ticker)
        data = yf.download(
            symbol.ticker,
            period=period,
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        return normalize_market_data(data, symbol.ticker)

    def fetch_history(
        self,
        symbol: SymbolConfig,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch daily historical data for backtesting and optimization."""

        padded_start = start_date - timedelta(days=max(symbol.average_days + 65, 420))
        self.logger.info(
            "Downloading historical data for %s from %s to %s",
            symbol.ticker,
            padded_start,
            end_date,
        )
        data = yf.download(
            symbol.ticker,
            start=padded_start.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        return normalize_market_data(data, symbol.ticker)


def normalize_market_data(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize yfinance output into a predictable single-ticker frame."""

    if data.empty:
        raise DataError(f"No data returned for {ticker}")
    if isinstance(data.columns, pd.MultiIndex):
        if ticker in data.columns.get_level_values(-1):
            data = data.xs(ticker, axis=1, level=-1)
        else:
            data.columns = data.columns.get_level_values(0)
    required = {"Open", "Close", "Volume"}
    missing = required - set(data.columns)
    if missing:
        raise DataError(f"{ticker} data missing columns: {sorted(missing)}")
    normalized = data.copy()
    normalized.index = pd.to_datetime(normalized.index)
    normalized = normalized.dropna(subset=["Close", "Volume"])
    if normalized.empty:
        raise DataError(f"No usable rows returned for {ticker}")
    return normalized


class AlertEngine:
    """Shared alert logic for live monitoring, backtesting, and optimization."""

    def evaluate(
        self,
        symbol: SymbolConfig,
        data: pd.DataFrame,
        row_position: int,
        triggered_at: datetime,
    ) -> AlertMetrics:
        """Evaluate one row of market data for an alert."""

        if row_position < symbol.average_days:
            raise DataError(f"{symbol.ticker}: not enough rows for average_days")

        row = data.iloc[row_position]
        average_window = data.iloc[row_position - symbol.average_days : row_position]
        average_volume = float(average_window["Volume"].mean())
        if average_volume <= 0:
            raise DataError(f"{symbol.ticker}: average volume is zero")

        current_volume = int(row["Volume"])
        percent_change = ((current_volume - average_volume) / average_volume) * 100
        rvol = current_volume / average_volume
        open_price = float(row["Open"])
        price = float(row["Close"])
        candle_color, candle_direction, candle_change_percent = self._candle_metrics(
            open_price,
            price,
        )
        volume_condition, direction = self._volume_condition(symbol, percent_change)

        price_change = self._price_change_percent(symbol, data, row_position, price)
        price_result = self._price_condition(symbol, price_change)
        final_result = (
            symbol.enabled
            and average_volume >= symbol.minimum_volume
            and volume_condition
            and price_result
        )
        rsi14 = self._rsi14(data, row_position)
        high_52_week = self._high_52_week(data, row_position)
        distance_from_52_week_high = (
            ((high_52_week - price) / high_52_week) * 100
            if high_52_week and high_52_week > 0
            else None
        )
        sma200 = self._sma(data, row_position, 200)
        distance_above_200ma = (
            ((price - sma200) / sma200) * 100 if sma200 and sma200 > 0 else None
        )
        profit_taking_score = self._profit_taking_score(
            percent_change,
            candle_color,
            distance_from_52_week_high,
            distance_above_200ma,
        )

        return AlertMetrics(
            ticker=symbol.ticker,
            triggered_at=triggered_at,
            volume=current_volume,
            average_volume=average_volume,
            percent_change=percent_change,
            rvol=rvol,
            open_price=open_price,
            price=price,
            candle_color=candle_color,
            candle_direction=candle_direction,
            candle_change_percent=candle_change_percent,
            price_change_percent=price_change,
            price_filter_enabled=symbol.price_filter.enabled,
            price_filter_result=price_result,
            volume_condition_result=volume_condition,
            final_alert_result=final_result,
            direction_triggered=direction,
            rsi14=rsi14,
            high_52_week=high_52_week,
            distance_from_52_week_high_percent=distance_from_52_week_high,
            sma200=sma200,
            distance_above_200ma_percent=distance_above_200ma,
            profit_taking_score=profit_taking_score,
        )

    def _candle_metrics(self, open_price: float, close_price: float) -> tuple[str, str, float]:
        """Return candle color, plain-English direction, and percent move."""

        if open_price == 0:
            change_percent = 0.0
        else:
            change_percent = ((close_price - open_price) / open_price) * 100

        if close_price > open_price:
            return "GREEN", "UP", change_percent
        if close_price < open_price:
            return "RED", "DOWN", change_percent
        return "NEUTRAL", "UNCHANGED", change_percent

    def _volume_condition(self, symbol: SymbolConfig, percent_change: float) -> tuple[bool, str]:
        """Evaluate configured volume direction."""

        upward = percent_change >= symbol.volume_percent
        downward = percent_change <= -symbol.volume_percent
        if symbol.direction == "up":
            return upward, "UP" if upward else ""
        if symbol.direction == "down":
            return downward, "DOWN" if downward else ""
        if upward:
            return True, "UP"
        if downward:
            return True, "DOWN"
        return False, ""

    def _price_change_percent(
        self,
        symbol: SymbolConfig,
        data: pd.DataFrame,
        row_position: int,
        price: float,
    ) -> float | None:
        """Calculate configured price reference change."""

        if not symbol.price_filter.enabled:
            return None

        reference = symbol.price_filter.reference
        if reference == "previous_close":
            if row_position < 1:
                return None
            base = float(data.iloc[row_position - 1]["Close"])
        elif reference == "20_day_moving_average":
            if row_position < 20:
                return None
            base = float(data.iloc[row_position - 20 : row_position]["Close"].mean())
        elif reference == "50_day_moving_average":
            if row_position < 50:
                return None
            base = float(data.iloc[row_position - 50 : row_position]["Close"].mean())
        else:
            return None

        if base == 0:
            return None
        return ((price - base) / base) * 100

    def _price_condition(self, symbol: SymbolConfig, price_change: float | None) -> bool:
        """Evaluate optional price filter."""

        if not symbol.price_filter.enabled:
            return True
        if price_change is None:
            return False
        threshold = symbol.price_filter.percent
        direction = symbol.price_filter.direction
        if direction == "up":
            return price_change >= threshold
        if direction == "down":
            return price_change <= -threshold
        return abs(price_change) >= threshold

    def _rsi14(self, data: pd.DataFrame, row_position: int) -> float | None:
        """Calculate standard 14-period RSI using Wilder smoothing."""

        if row_position < 14:
            return None
        closes = data.iloc[: row_position + 1]["Close"].astype(float)
        deltas = closes.diff()
        gains = deltas.clip(lower=0)
        losses = -deltas.clip(upper=0)
        average_gain = gains.iloc[1:15].mean()
        average_loss = losses.iloc[1:15].mean()
        for index in range(15, len(closes)):
            average_gain = ((average_gain * 13) + gains.iloc[index]) / 14
            average_loss = ((average_loss * 13) + losses.iloc[index]) / 14
        if average_loss == 0:
            return 100.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    def _high_52_week(self, data: pd.DataFrame, row_position: int) -> float | None:
        """Return highest close over the current and prior 251 trading days."""

        if row_position < 251:
            return None
        return float(data.iloc[row_position - 251 : row_position + 1]["Close"].max())

    def _sma(self, data: pd.DataFrame, row_position: int, days: int) -> float | None:
        """Return simple moving average over the current and prior window."""

        if row_position < days - 1:
            return None
        return float(data.iloc[row_position - days + 1 : row_position + 1]["Close"].mean())

    def _profit_taking_score(
        self,
        volume_spike_percent: float,
        candle_color: str,
        distance_from_52_week_high_percent: float | None,
        distance_above_200ma_percent: float | None,
    ) -> int:
        """Score whether VTI is extended enough for profit taking."""

        score = 0
        if volume_spike_percent > 100:
            score += 1
        if candle_color == "RED":
            score += 1
        if (
            distance_from_52_week_high_percent is not None
            and distance_from_52_week_high_percent <= 2
        ):
            score += 1
        if (
            distance_above_200ma_percent is not None
            and distance_above_200ma_percent >= 10
        ):
            score += 1
        return score


class NotificationService:
    """Sends email and SMS notifications."""

    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger

    def send_alert(self, metrics: AlertMetrics) -> None:
        """Send one alert to configured recipients."""

        subject = (
            f"{metrics.strategy_alert_type} ALERT: {metrics.ticker}"
            if metrics.strategy_alert_type
            else f"VOLUME ALERT: {metrics.ticker}"
        )
        body = format_alert_body(metrics, self.config.schedule.timezone)
        self._send(subject, body, self.all_recipients())

    def send_daily_summary(self, alerts: Sequence[AlertMetrics]) -> None:
        """Email a market-close summary of alerts."""

        if not alerts:
            self.logger.info("No alerts for daily summary")
            return
        lines = ["Daily Volume Alert Summary", ""]
        for alert in alerts:
            price_change = format_optional_percent(alert.price_change_percent)
            rsi14 = f"{alert.rsi14:.1f}" if alert.rsi14 is not None else "N/A"
            lines.append(
                (
                    f"{alert.ticker} | {alert.triggered_at.strftime('%H:%M')} | "
                    f"Volume {alert.volume:,} | Avg {alert.average_volume:,.0f} | "
                    f"Change {alert.percent_change:+.1f}% | RVOL {alert.rvol:.2f} | "
                    f"Candle {alert.candle_color} {alert.candle_change_percent:+.1f}% | "
                    f"Price Change {price_change} | RSI14 {rsi14} | "
                    f"Score {alert.profit_taking_score} | State {alert.position_state}"
                )
            )
        self._send("Daily Volume Alert Summary", "\n".join(lines), self.config.notifications.emails)

    def all_recipients(self) -> list[str]:
        """Return email plus SMS gateway recipients."""

        recipients = list(self.config.notifications.emails)
        for text in self.config.notifications.texts:
            digits = "".join(character for character in text.number if character.isdigit())
            recipients.append(f"{digits}@{SMS_GATEWAYS[text.carrier]}")
        return recipients

    def _send(self, subject: str, body: str, recipients: Sequence[str]) -> None:
        """Send an email message to recipients."""

        if not recipients:
            self.logger.warning("No recipients configured for message: %s", subject)
            return
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.smtp.username
        message["To"] = ", ".join(recipients)
        message.set_content(body)
        try:
            with smtplib.SMTP(self.config.smtp.server, self.config.smtp.port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.config.smtp.username, self.config.smtp.password)
                smtp.send_message(message)
            self.logger.info("Sent notification: %s", subject)
        except OSError as exc:
            self.logger.error("SMTP failure for %s: %s", subject, exc)


def format_optional_percent(value: float | None) -> str:
    """Format optional percentage values for output."""

    if value is None:
        return "N/A"
    return f"{value:+.1f}%"


def format_optional_plain_percent(value: float | None) -> str:
    """Format optional percentage values without a forced sign."""

    if value is None:
        return "N/A"
    return f"{value:.1f}%"


def apply_strategy_state(
    metrics: AlertMetrics,
    position_state: str,
    sell_price: float | None,
) -> AlertMetrics:
    """Apply Sell Half / Rebuy Lower strategy state to one evaluated row."""

    if position_state == POSITION_WAITING_FOR_REBUY and sell_price is not None:
        ten_percent_correction = metrics.price <= sell_price * 0.90
        reversal_after_decline = (
            metrics.price <= sell_price * 0.93
            and metrics.percent_change > 100
            and metrics.candle_color == "GREEN"
        )
        if ten_percent_correction:
            return replace(
                metrics,
                position_state=POSITION_NORMAL,
                strategy_alert_type=STRATEGY_REBUY,
                strategy_reasons=("10% correction achieved",),
                sell_price=sell_price,
                rebuy_reason="10% correction achieved",
            )
        if reversal_after_decline:
            return replace(
                metrics,
                position_state=POSITION_NORMAL,
                strategy_alert_type=STRATEGY_REBUY,
                strategy_reasons=("High-volume reversal after meaningful decline",),
                sell_price=sell_price,
                rebuy_reason="High-volume reversal after meaningful decline",
            )
        return replace(
            metrics,
            position_state=POSITION_WAITING_FOR_REBUY,
            sell_price=sell_price,
        )

    if metrics.profit_taking_score >= 3:
        reasons = profit_taking_reasons(metrics)
        return replace(
            metrics,
            position_state=POSITION_WAITING_FOR_REBUY,
            strategy_alert_type=STRATEGY_SELL_HALF,
            strategy_reasons=tuple(reasons),
            sell_price=metrics.price,
        )

    return replace(metrics, position_state=POSITION_NORMAL, sell_price=None)


def profit_taking_reasons(metrics: AlertMetrics) -> list[str]:
    """Return human-readable SELL_HALF reasons from the active score inputs."""

    reasons: list[str] = []
    if metrics.percent_change > 100 and metrics.candle_color == "RED":
        reasons.append("High volume distribution detected")
    elif metrics.percent_change > 100:
        reasons.append("Volume spike above 100%")
    elif metrics.candle_color == "RED":
        reasons.append("Red candle distribution")
    if (
        metrics.distance_from_52_week_high_percent is not None
        and metrics.distance_from_52_week_high_percent <= 2
    ):
        reasons.append("Near 52-week high")
    if (
        metrics.distance_above_200ma_percent is not None
        and metrics.distance_above_200ma_percent >= 10
    ):
        reasons.append("Extended above 200-day moving average")
    return reasons


def format_alert_body(metrics: AlertMetrics, timezone: str) -> str:
    """Format the required alert message body."""

    if metrics.strategy_alert_type == STRATEGY_SELL_HALF:
        return format_sell_half_alert_body(metrics)
    if metrics.strategy_alert_type == STRATEGY_REBUY:
        return format_rebuy_alert_body(metrics)

    price_change = format_optional_percent(metrics.price_change_percent)
    return "\n".join(
        [
            f"Ticker: {metrics.ticker}",
            "",
            "Current Volume:",
            f"{metrics.volume:,}",
            "",
            "Average Volume:",
            f"{metrics.average_volume:,.0f}",
            "",
            "Percent Change:",
            f"{metrics.percent_change:+.1f}%",
            "",
            "RVOL:",
            f"{metrics.rvol:.2f}",
            "",
            "Price:",
            f"{metrics.price:.2f}",
            "",
            "Candle:",
            f"{metrics.candle_color} / {metrics.candle_direction}",
            "",
            "Open:",
            f"{metrics.open_price:.2f}",
            "",
            "Close:",
            f"{metrics.price:.2f}",
            "",
            "Candle Change:",
            f"{metrics.candle_change_percent:+.1f}%",
            "",
            "Price Change:",
            price_change,
            "",
            "RSI14:",
            f"{metrics.rsi14:.1f}" if metrics.rsi14 is not None else "N/A",
            "",
            "Distance From 52-Week High:",
            format_optional_plain_percent(metrics.distance_from_52_week_high_percent),
            "",
            "Distance Above 200-Day MA:",
            format_optional_percent(metrics.distance_above_200ma_percent),
            "",
            "Profit Taking Score:",
            str(metrics.profit_taking_score),
            "",
            "Position State:",
            metrics.position_state,
            "",
            "Direction:",
            metrics.direction_triggered,
            "",
            "Triggered:",
            f"{metrics.triggered_at.strftime('%Y-%m-%d %H:%M:%S')} {timezone}",
        ]
    )


def format_sell_half_alert_body(metrics: AlertMetrics) -> str:
    """Format the SELL_HALF alert message."""

    reasons = [f"* {reason}" for reason in metrics.strategy_reasons]
    return "\n".join(
        [
            "SELL HALF ALERT",
            "",
            f"Ticker: {metrics.ticker}",
            f"Close: ${metrics.price:.2f}",
            "",
            "Reasons:",
            "",
            *reasons,
            "",
            "Suggested Action:",
            "Consider selling 50% of position and wait for a correction before rebuying.",
            "",
            f"RSI14: {metrics.rsi14:.1f}" if metrics.rsi14 is not None else "RSI14: N/A",
            (
                "DistanceFrom52WeekHighPercent: "
                f"{format_optional_plain_percent(metrics.distance_from_52_week_high_percent)}"
            ),
            (
                "DistanceAbove200MAPercent: "
                f"{format_optional_percent(metrics.distance_above_200ma_percent)}"
            ),
            f"ProfitTakingScore: {metrics.profit_taking_score}",
            f"PositionState: {metrics.position_state}",
        ]
    )


def format_rebuy_alert_body(metrics: AlertMetrics) -> str:
    """Format the REBUY alert message."""

    reasons = [f"* {reason}" for reason in metrics.strategy_reasons]
    return "\n".join(
        [
            "REBUY ALERT",
            "",
            f"Ticker: {metrics.ticker}",
            f"Close: ${metrics.price:.2f}",
            "",
            "Reason:",
            "",
            *reasons,
            "",
            "Suggested Action:",
            "Consider repurchasing previously sold shares.",
            "",
            f"SellPrice: ${metrics.sell_price:.2f}" if metrics.sell_price else "SellPrice: N/A",
            f"PositionState: {metrics.position_state}",
        ]
    )


class LiveMonitor:
    """Coordinates live scans and notification delivery."""

    def __init__(
        self,
        config: AppConfig,
        data_provider: MarketDataProvider,
        engine: AlertEngine,
        history: AlertHistory,
        notifier: NotificationService,
        calendar: MarketCalendar,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.data_provider = data_provider
        self.engine = engine
        self.history = history
        self.notifier = notifier
        self.calendar = calendar
        self.logger = logger
        self.daily_alerts: list[AlertMetrics] = []

    def scan_once(self) -> ScanResult:
        """Run one live scan over enabled symbols."""

        result = ScanResult()
        now = self.calendar.now()
        for symbol in self.config.symbols:
            if not symbol.enabled:
                continue
            result.evaluated += 1
            try:
                data = self.data_provider.fetch_live(symbol)
                metrics = self.engine.evaluate(symbol, data, len(data) - 1, now)
                position_state, sell_price = self.history.strategy_state(symbol.ticker)
                metrics = apply_strategy_state(metrics, position_state, sell_price)
                if metrics.strategy_alert_type:
                    self.notifier.send_alert(metrics)
                    self.history.record(metrics)
                    self.daily_alerts.append(metrics)
                    result.alerts.append(metrics)
                    self.logger.info(
                        "%s triggered for %s",
                        metrics.strategy_alert_type,
                        symbol.ticker,
                    )
                elif self._should_send(symbol, metrics, now):
                    self.notifier.send_alert(metrics)
                    self.history.record(metrics)
                    self.daily_alerts.append(metrics)
                    result.alerts.append(metrics)
                    self.logger.info("Alert triggered for %s", symbol.ticker)
                else:
                    self.logger.info("No alert for %s", symbol.ticker)
            except (DataError, OSError, ValueError) as exc:
                result.errors += 1
                self.logger.error("Failed processing %s: %s", symbol.ticker, exc)
        return result

    def run(self) -> None:
        """Run scans during market hours until configured market close."""

        self.logger.info("Starting live monitor")
        while True:
            now = self.calendar.now()
            if self.calendar.is_market_open(now):
                self.scan_once()
                sleep_seconds = self.config.schedule.interval_minutes * 60
                next_scan = self.calendar.now() + timedelta(seconds=sleep_seconds)
                if next_scan > self.calendar.market_end_today(now):
                    break
                time.sleep(sleep_seconds)
            else:
                if now > self.calendar.market_end_today(now):
                    break
                self.logger.info("Market not open; sleeping until next scan window")
                time.sleep(60)
        self.notifier.send_daily_summary(self.daily_alerts)
        self.logger.info("Live monitor stopped")

    def _should_send(
        self,
        symbol: SymbolConfig,
        metrics: AlertMetrics,
        now: datetime,
    ) -> bool:
        """Apply duplicate and cooldown checks after engine evaluation."""

        if not metrics.final_alert_result:
            return False
        if not symbol.repeat_alerts and self.history.has_duplicate(
            symbol.ticker,
            now.date(),
            metrics.direction_triggered,
        ):
            self.logger.info("Duplicate alert suppressed for %s", symbol.ticker)
            return False
        if self.history.is_in_cooldown(
            symbol.ticker,
            now,
            self.config.general.alert_cooldown_minutes,
        ):
            self.logger.info("Cooldown suppressed alert for %s", symbol.ticker)
            return False
        return True


class Backtester:
    """Runs historical backtests and threshold optimization."""

    def __init__(
        self,
        config: AppConfig,
        data_provider: MarketDataProvider,
        engine: AlertEngine,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.data_provider = data_provider
        self.engine = engine
        self.logger = logger
        self.timezone = ZoneInfo(config.schedule.timezone)

    def run(
        self,
        ticker: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> BacktestResult:
        """Run a backtest and write all configured outputs."""

        end = end_date or datetime.now(self.timezone).date()
        start = start_date or end - timedelta(days=365 * self.config.backtest.years)
        symbols = self._select_symbols(ticker)
        all_details: list[dict[str, Any]] = []
        all_cooldown: list[dict[str, Any]] = []
        all_strategy_pairs: list[dict[str, Any]] = []
        data_by_ticker: dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            try:
                data = self.data_provider.fetch_history(symbol, start, end)
                data_by_ticker[symbol.ticker] = data
                details, cooldown, strategy_pairs = self._run_symbol(symbol, data, start, end)
                all_details.extend(details)
                all_cooldown.append(cooldown)
                all_strategy_pairs.extend(strategy_pairs)
            except (DataError, OSError, ValueError) as exc:
                self.logger.error("Backtest failed for %s: %s", symbol.ticker, exc)

        summary = build_summary(all_details, all_strategy_pairs)
        write_csv(REPORTS_DIR / "backtest_details.csv", all_details)
        write_csv(REPORTS_DIR / "backtest_summary.csv", summary)
        write_csv(REPORTS_DIR / "cooldown_analysis.csv", all_cooldown)
        write_csv(REPORTS_DIR / "strategy_pairs.csv", all_strategy_pairs)
        if self.config.backtest.generate_html_report:
            write_html_report(
                REPORTS_DIR / "backtest_report.html",
                "Backtest Report",
                summary,
                all_details,
            )
            write_html_report(
                REPORTS_DIR / "strategy_pairs.html",
                "Sell Half / Rebuy Pairs",
                summary,
                all_strategy_pairs,
            )
        if self.config.backtest.generate_charts:
            for symbol in symbols:
                data = data_by_ticker.get(symbol.ticker)
                if data is not None:
                    create_charts(symbol, data, all_details)
        print_summary(summary)
        return BacktestResult(
            details=all_details,
            summary=summary,
            cooldown=all_cooldown,
            strategy_pairs=all_strategy_pairs,
        )

    def optimize(
        self,
        ticker: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Run threshold optimization and write reports."""

        end = end_date or datetime.now(self.timezone).date()
        start = start_date or end - timedelta(days=365 * self.config.backtest.years)
        rows: list[dict[str, Any]] = []
        alert_rows: list[dict[str, Any]] = []
        for base_symbol in self._select_symbols(ticker):
            try:
                data = self.data_provider.fetch_history(base_symbol, start, end)
            except (DataError, OSError, ValueError) as exc:
                self.logger.error(
                    "Optimization download failed for %s: %s",
                    base_symbol.ticker,
                    exc,
                )
                continue

            price_thresholds = (
                OPTIMIZE_PRICE_THRESHOLDS if base_symbol.price_filter.enabled else [None]
            )
            for volume_threshold in OPTIMIZE_VOLUME_THRESHOLDS:
                for price_threshold in price_thresholds:
                    price_filter = base_symbol.price_filter
                    if price_threshold is not None:
                        price_filter = replace(price_filter, percent=float(price_threshold))
                    symbol = replace(
                        base_symbol,
                        volume_percent=float(volume_threshold),
                        price_filter=price_filter,
                    )
                    details, _, strategy_pairs = self._run_symbol(symbol, data, start, end)
                    summary = build_summary(details, strategy_pairs)
                    ticker_summary = summary[0] if summary else empty_summary(symbol.ticker)
                    alert_dates = alert_dates_from_details(details)
                    alert_rows.extend(
                        optimization_alert_rows(
                            details,
                            volume_threshold,
                            price_threshold,
                        )
                    )
                    rows.append(
                        {
                            "Ticker": symbol.ticker,
                            "VolumeThresholdPercent": volume_threshold,
                            "PricePercent": price_threshold if price_threshold is not None else "",
                            "TotalAlerts": ticker_summary["Total Alerts"],
                            "AlertsPerYear": ticker_summary["Alerts Per Year"],
                            "AverageRVOL": ticker_summary["Average RVOL"],
                            "MaximumRVOL": ticker_summary["Maximum RVOL"],
                            "LargestSpike": ticker_summary["Largest Positive Spike"],
                            "FirstAlertDate": alert_dates[0] if alert_dates else "",
                            "LastAlertDate": alert_dates[-1] if alert_dates else "",
                            "AlertDateCount": len(alert_dates),
                        }
                    )

        alert_rows_by_date = sort_optimization_alerts_by_date(alert_rows)
        write_csv(REPORTS_DIR / "optimization_report.csv", rows)
        write_csv(REPORTS_DIR / "optimization_alerts.csv", alert_rows_by_date)
        write_html_report(
            REPORTS_DIR / "optimization_report.html",
            "Optimization Report",
            rows,
            alert_rows_by_date,
        )
        write_html_report(
            REPORTS_DIR / "optimization_alerts.html",
            "Optimization Alert Dates",
            alert_rows_by_date,
            [],
        )
        return rows

    def _select_symbols(self, ticker: str | None) -> list[SymbolConfig]:
        """Filter configured symbols by optional ticker."""

        symbols = [symbol for symbol in self.config.symbols if symbol.enabled]
        if ticker is None:
            return symbols
        selected = [symbol for symbol in symbols if symbol.ticker == ticker.upper()]
        if not selected:
            raise ConfigError(f"Ticker not found or disabled: {ticker}")
        return selected

    def _run_symbol(
        self,
        symbol: SymbolConfig,
        data: pd.DataFrame,
        start_date: date,
        end_date: date,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        """Run a symbol-level daily backtest."""

        details: list[dict[str, Any]] = []
        strategy_pairs: list[dict[str, Any]] = []
        potential_alerts = 0
        suppressed_alerts = 0
        final_alerts = 0
        last_alert: datetime | None = None
        seen_daily: set[tuple[str, str, str]] = set()
        position_state = POSITION_NORMAL
        sell_price: float | None = None
        sell_date: date | None = None

        for position in range(len(data)):
            index_date = data.index[position].date()
            if index_date < start_date or index_date > end_date:
                continue
            triggered_at = datetime.combine(
                index_date,
                self.config.schedule.market_end,
                self.timezone,
            )
            try:
                metrics = self.engine.evaluate(symbol, data, position, triggered_at)
            except DataError:
                continue
            metrics = apply_strategy_state(metrics, position_state, sell_price)

            potential = metrics.final_alert_result
            final = False
            if potential:
                potential_alerts += 1
                duplicate_key = (index_date.isoformat(), symbol.ticker, metrics.direction_triggered)
                in_cooldown = (
                    last_alert is not None
                    and triggered_at - last_alert
                    < timedelta(minutes=self.config.general.alert_cooldown_minutes)
                )
                duplicate = not symbol.repeat_alerts and duplicate_key in seen_daily
                final = not in_cooldown and not duplicate
                if final:
                    final_alerts += 1
                    last_alert = triggered_at
                    seen_daily.add(duplicate_key)
                else:
                    suppressed_alerts += 1

            if metrics.strategy_alert_type == STRATEGY_SELL_HALF:
                position_state = POSITION_WAITING_FOR_REBUY
                sell_price = metrics.sell_price
                sell_date = index_date
            elif metrics.strategy_alert_type == STRATEGY_REBUY:
                if sell_date is not None and sell_price is not None:
                    captured = ((sell_price - metrics.price) / sell_price) * 100
                    strategy_pairs.append(
                        {
                            "Ticker": symbol.ticker,
                            "SellDate": sell_date.isoformat(),
                            "RebuyDate": index_date.isoformat(),
                            "DaysBetween": (index_date - sell_date).days,
                            "SellPrice": round(sell_price, 2),
                            "RebuyPrice": round(metrics.price, 2),
                            "PullbackCapturedPercent": round(captured, 2),
                            "GainFromRoundTripPercent": round(captured, 2),
                            "RebuyReason": metrics.rebuy_reason,
                        }
                    )
                position_state = POSITION_NORMAL
                sell_price = None
                sell_date = None

            details.append(backtest_row(metrics, final))

        suppression_percentage = (
            (suppressed_alerts / potential_alerts) * 100 if potential_alerts else 0
        )
        cooldown = {
            "Ticker": symbol.ticker,
            "PotentialAlerts": potential_alerts,
            "SuppressedAlerts": suppressed_alerts,
            "FinalAlerts": final_alerts,
            "SuppressionPercentage": round(suppression_percentage, 2),
        }
        return details, cooldown, strategy_pairs


def backtest_row(metrics: AlertMetrics, final_alert: bool) -> dict[str, Any]:
    """Convert metrics into the required detail report row."""

    return {
        "Date": metrics.triggered_at.date().isoformat(),
        "Ticker": metrics.ticker,
        "Volume": metrics.volume,
        "AverageVolume": round(metrics.average_volume, 2),
        "PercentChange": round(metrics.percent_change, 2),
        "RVOL": round(metrics.rvol, 4),
        "Open": round(metrics.open_price, 2),
        "Price": round(metrics.price, 2),
        "CandleColor": metrics.candle_color,
        "CandleDirection": metrics.candle_direction,
        "CandleChangePercent": round(metrics.candle_change_percent, 2),
        "PriceChangePercent": (
            round(metrics.price_change_percent, 2)
            if metrics.price_change_percent is not None
            else ""
        ),
        "PriceFilterEnabled": metrics.price_filter_enabled,
        "PriceFilterResult": metrics.price_filter_result,
        "VolumeConditionResult": metrics.volume_condition_result,
        "FinalAlertResult": final_alert,
        "DirectionTriggered": metrics.candle_direction,
        "RSI14": round(metrics.rsi14, 2) if metrics.rsi14 is not None else "",
        "High52Week": round(metrics.high_52_week, 2) if metrics.high_52_week is not None else "",
        "DistanceFrom52WeekHighPercent": (
            round(metrics.distance_from_52_week_high_percent, 2)
            if metrics.distance_from_52_week_high_percent is not None
            else ""
        ),
        "SMA200": round(metrics.sma200, 2) if metrics.sma200 is not None else "",
        "DistanceAbove200MAPercent": (
            round(metrics.distance_above_200ma_percent, 2)
            if metrics.distance_above_200ma_percent is not None
            else ""
        ),
        "ProfitTakingScore": metrics.profit_taking_score,
        "PositionState": metrics.position_state,
        "StrategyAlert": metrics.strategy_alert_type,
        "StrategyReasons": "; ".join(metrics.strategy_reasons),
        "SellPrice": round(metrics.sell_price, 2) if metrics.sell_price is not None else "",
        "RebuyReason": metrics.rebuy_reason,
    }


def alert_dates_from_details(details: Sequence[dict[str, Any]]) -> list[str]:
    """Return sorted dates where final alerts fired."""

    dates = {
        str(row["Date"])
        for row in details
        if row.get("FinalAlertResult") is True and row.get("Date")
    }
    return sorted(dates)


def optimization_alert_rows(
    details: Sequence[dict[str, Any]],
    volume_threshold: int,
    price_threshold: int | None,
) -> list[dict[str, Any]]:
    """Return one optimization detail row for each alert date."""

    rows = [
        row
        for row in details
        if row.get("FinalAlertResult") is True and row.get("Date")
    ]
    rows.sort(key=lambda row: str(row["Date"]))
    return [
        {
            "Ticker": row["Ticker"],
            "Date": row["Date"],
            "VolumeThresholdPercent": volume_threshold,
            "PricePercent": price_threshold if price_threshold is not None else "",
            "ActualVolumePercentChange": row["PercentChange"],
            "RVOL": row["RVOL"],
            "Volume": row["Volume"],
            "AverageVolume": row["AverageVolume"],
            "Open": row["Open"],
            "Close": row["Price"],
            "CandleColor": row["CandleColor"],
            "CandleDirection": row["CandleDirection"],
            "CandleChangePercent": row["CandleChangePercent"],
            "DirectionTriggered": row["CandleDirection"],
            "RSI14": row.get("RSI14", ""),
            "DistanceFrom52WeekHighPercent": row.get("DistanceFrom52WeekHighPercent", ""),
            "DistanceAbove200MAPercent": row.get("DistanceAbove200MAPercent", ""),
            "ProfitTakingScore": row.get("ProfitTakingScore", ""),
            "PositionState": row.get("PositionState", ""),
            "StrategyAlert": row.get("StrategyAlert", ""),
        }
        for row in rows
    ]


def sort_optimization_alerts_by_date(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort optimization alert details as a calendar-first timeline."""

    return sorted(
        rows,
        key=lambda row: (
            str(row.get("Date", "")),
            str(row.get("Ticker", "")),
            float(row.get("VolumeThresholdPercent") or 0),
            float(row.get("PricePercent") or 0),
        ),
    )


def build_summary(
    details: Sequence[dict[str, Any]],
    strategy_pairs: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-ticker summary statistics."""

    if not details:
        return []
    frame = pd.DataFrame(details)
    pairs_frame = pd.DataFrame(strategy_pairs or [])
    rows: list[dict[str, Any]] = []
    for ticker, group in frame.groupby("Ticker"):
        alert_group = group[group["FinalAlertResult"] == True]  # noqa: E712
        sell_half_group = group[group["StrategyAlert"] == STRATEGY_SELL_HALF]
        rebuy_group = group[group["StrategyAlert"] == STRATEGY_REBUY]
        ticker_pairs = (
            pairs_frame[pairs_frame["Ticker"] == ticker]
            if not pairs_frame.empty
            else pd.DataFrame()
        )
        trading_days = int(group["Date"].nunique())
        years = max(trading_days / 252, 1 / 252)
        total_alerts = int(len(alert_group))
        average_days_between = (
            round(float(ticker_pairs["DaysBetween"].mean()), 2)
            if not ticker_pairs.empty
            else 0
        )
        average_pullback = (
            round(float(ticker_pairs["PullbackCapturedPercent"].mean()), 2)
            if not ticker_pairs.empty
            else 0
        )
        largest_pullback = (
            round(float(ticker_pairs["PullbackCapturedPercent"].max()), 2)
            if not ticker_pairs.empty
            else 0
        )
        average_gain = (
            round(float(ticker_pairs["GainFromRoundTripPercent"].mean()), 2)
            if not ticker_pairs.empty
            else 0
        )
        rows.append(
            {
                "Ticker": ticker,
                "Total Trading Days": trading_days,
                "Total Alerts": total_alerts,
                "Alerts Per Year": round(total_alerts / years, 2),
                "Alert Frequency": round(total_alerts / trading_days, 4) if trading_days else 0,
                "Average RVOL": round(float(group["RVOL"].mean()), 4),
                "Maximum RVOL": round(float(group["RVOL"].max()), 4),
                "Minimum RVOL": round(float(group["RVOL"].min()), 4),
                "Largest Positive Spike": round(float(group["PercentChange"].max()), 2),
                "Largest Negative Spike": round(float(group["PercentChange"].min()), 2),
                "Total SELL_HALF Alerts": int(len(sell_half_group)),
                "Total REBUY Alerts": int(len(rebuy_group)),
                "Average Days Between Sell and Rebuy": average_days_between,
                "Average Pullback Captured": average_pullback,
                "Largest Pullback Captured": largest_pullback,
                "Average Gain From Rebuy Strategy": average_gain,
            }
        )
    return rows


def empty_summary(ticker: str) -> dict[str, Any]:
    """Return an empty summary row for optimization edge cases."""

    return {
        "Ticker": ticker,
        "Total Trading Days": 0,
        "Total Alerts": 0,
        "Alerts Per Year": 0,
        "Alert Frequency": 0,
        "Average RVOL": 0,
        "Maximum RVOL": 0,
        "Minimum RVOL": 0,
        "Largest Positive Spike": 0,
        "Largest Negative Spike": 0,
        "Total SELL_HALF Alerts": 0,
        "Total REBUY Alerts": 0,
        "Average Days Between Sell and Rebuy": 0,
        "Average Pullback Captured": 0,
        "Largest Pullback Captured": 0,
        "Average Gain From Rebuy Strategy": 0,
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Write rows to a CSV file, preserving empty output files."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not fieldnames:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_html_report(
    path: Path,
    title: str,
    summary_rows: Sequence[dict[str, Any]],
    detail_rows: Sequence[dict[str, Any]],
) -> None:
    """Write a simple HTML report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    summary_html = html_table(summary_rows) if summary_rows else "<p>No rows.</p>"
    details_html = html_table(detail_rows) if detail_rows else "<p>No detail rows.</p>"
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #222; }}
    table {{ border-collapse: collapse; margin-bottom: 32px; width: 100%; }}
    th, td {{
      border: 1px solid #ddd;
      padding: 6px 8px;
      text-align: right;
      white-space: nowrap;
    }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f2f2f2; }}
    .threshold-100 {{ background: #fee2e2; color: #7f1d1d; font-weight: 700; }}
    .threshold-150 {{ background: #fca5a5; color: #7f1d1d; font-weight: 700; }}
    .threshold-200 {{ background: #dc2626; color: #fff; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <h2>Summary</h2>
  {summary_html}
  <h2>Details</h2>
  {details_html}
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def html_table(rows: Sequence[dict[str, Any]]) -> str:
    """Render rows as an HTML table with optimization threshold styling."""

    if not rows:
        return "<p>No rows.</p>"

    columns = list(rows[0].keys())
    header = "".join(f"<th>{escape(str(column))}</th>" for column in columns)
    body_rows = []
    for row in rows:
        row_class = threshold_row_class(row)
        row_class_attribute = f' class="{row_class}"' if row_class else ""
        cells = []
        for column in columns:
            value = row.get(column, "")
            cells.append(f"<td>{escape(str(value))}</td>")
        body_rows.append(f"<tr{row_class_attribute}>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def threshold_row_class(row: dict[str, Any]) -> str:
    """Return CSS class for rows with notable volume thresholds."""

    try:
        threshold = int(float(row.get("VolumeThresholdPercent", "")))
    except (TypeError, ValueError):
        return ""
    if threshold in {100, 150, 200}:
        return f"threshold-{threshold}"
    return ""


def create_charts(
    symbol: SymbolConfig,
    data: pd.DataFrame,
    detail_rows: Sequence[dict[str, Any]],
) -> None:
    """Generate required charts for a ticker."""

    if plt is None:
        print("Skipping charts: matplotlib is not installed.")
        return

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    ticker_details = [row for row in detail_rows if row["Ticker"] == symbol.ticker]
    frame = data.copy()
    frame["AverageVolume"] = frame["Volume"].rolling(symbol.average_days).mean()
    frame["RVOL"] = frame["Volume"] / frame["AverageVolume"]
    detail_frame = pd.DataFrame(ticker_details)
    alert_dates = (
        pd.to_datetime(detail_frame[detail_frame["FinalAlertResult"] == True]["Date"])  # noqa: E712
        if not detail_frame.empty
        else pd.Series(dtype="datetime64[ns]")
    )

    save_line_chart(symbol.ticker, frame.index, frame["Volume"], "Volume History", "volume_history")
    save_line_chart(
        symbol.ticker,
        frame.index,
        frame["AverageVolume"],
        "Average Volume",
        "average_volume",
    )
    save_line_chart(symbol.ticker, frame.index, frame["RVOL"], "RVOL History", "rvol_history")

    plt.figure(figsize=(10, 5))
    plt.plot(frame.index, frame["Volume"], label="Volume")
    if not alert_dates.empty:
        alert_values = frame.reindex(alert_dates)["Volume"]
        plt.scatter(alert_dates, alert_values, color="red", label="Alerts")
    plt.title(f"{symbol.ticker} Alert Occurrences")
    plt.legend()
    plt.tight_layout()
    plt.savefig(CHARTS_DIR / f"{symbol.ticker}_alert_occurrences.png")
    plt.close()

    if not detail_frame.empty:
        plt.figure(figsize=(10, 5))
        detail_frame["PercentChange"].hist(bins=30)
        plt.title(f"{symbol.ticker} Distribution of Volume Spikes")
        plt.xlabel("Percent Change")
        plt.ylabel("Frequency")
        plt.tight_layout()
        plt.savefig(CHARTS_DIR / f"{symbol.ticker}_volume_spike_distribution.png")
        plt.close()


def save_line_chart(
    ticker: str,
    x_values: Iterable[Any],
    y_values: Iterable[Any],
    title: str,
    filename_suffix: str,
) -> None:
    """Save a simple line chart."""

    plt.figure(figsize=(10, 5))
    plt.plot(list(x_values), list(y_values))
    plt.title(f"{ticker} {title}")
    plt.tight_layout()
    plt.savefig(CHARTS_DIR / f"{ticker}_{filename_suffix}.png")
    plt.close()


def print_summary(summary: Sequence[dict[str, Any]]) -> None:
    """Print backtest summary statistics to console."""

    if not summary:
        print("No summary rows generated.")
        return
    frame = pd.DataFrame(summary)
    print(frame.to_string(index=False))


def parse_date(value: str | None) -> date | None:
    """Parse an optional ISO date argument."""

    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_services(config: AppConfig, logger: logging.Logger) -> tuple[
    MarketDataProvider,
    AlertEngine,
    AlertHistory,
    NotificationService,
    MarketCalendar,
]:
    """Build application services."""

    return (
        MarketDataProvider(logger),
        AlertEngine(),
        AlertHistory(),
        NotificationService(config, logger),
        MarketCalendar(config),
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description="Monitor stocks and ETFs for unusual volume.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one scan and exit.")
    mode.add_argument("--backtest", action="store_true", help="Run historical backtest.")
    mode.add_argument("--optimize", action="store_true", help="Run threshold optimization.")
    parser.add_argument("--ticker", help="Limit backtest or optimization to one ticker.")
    parser.add_argument("--start-date", help="Backtest/optimization start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Backtest/optimization end date, YYYY-MM-DD.")
    return parser.parse_args()


def main() -> int:
    """Application entry point."""

    ensure_directories()
    logger = setup_logging("INFO")
    try:
        config = load_config()
        logger = setup_logging(config.general.log_level)
        logger.info("Starting %s", APP_NAME)
        data_provider, engine, history, notifier, calendar = build_services(config, logger)
        args = parse_args()
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)

        if args.backtest:
            logger.info("Running backtest")
            Backtester(config, data_provider, engine, logger).run(
                ticker=args.ticker,
                start_date=start_date,
                end_date=end_date,
            )
        elif args.optimize:
            logger.info("Running optimization")
            rows = Backtester(config, data_provider, engine, logger).optimize(
                ticker=args.ticker,
                start_date=start_date,
                end_date=end_date,
            )
            print(f"Optimization rows generated: {len(rows)}")
        elif args.once:
            logger.info("Running one scan")
            result = LiveMonitor(
                config,
                data_provider,
                engine,
                history,
                notifier,
                calendar,
                logger,
            ).scan_once()
            print(
                f"Scan complete: evaluated={result.evaluated}, "
                f"alerts={len(result.alerts)}, errors={result.errors}"
            )
        else:
            LiveMonitor(
                config,
                data_provider,
                engine,
                history,
                notifier,
                calendar,
                logger,
            ).run()
        logger.info("Shutdown complete")
        return 0
    except (ConfigError, KeyError, OSError, ValueError) as exc:
        logger.error("Fatal error: %s", exc)
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
