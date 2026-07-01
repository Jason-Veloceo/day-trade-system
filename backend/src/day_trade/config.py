"""Centralised configuration. Reads from .env at the repo root.

Anything that varies between environments lives here. Anything Ross-specific
(thresholds, cooldown windows) is configurable so we can tune without code
changes once we have live data.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_env: str = "development"
    log_level: str = "INFO"

    # --- Database ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5434/daytrade",
        description="Async SQLAlchemy URL (asyncpg driver).",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://postgres:postgres@localhost:5434/daytrade",
        description="Sync SQLAlchemy URL (psycopg2 driver) used by Alembic and one-shot scripts.",
    )

    # --- FastAPI ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- DTD ingestion ---
    dtd_playwright_profile_dir: str = "./playwright_profile"
    # WT member dashboard. From here the user clicks "Click here to Enter" -> chat-room-access
    # -> "Click here to Enter the Platform" -> accept disclaimer -> DTD chatroom loads, which
    # may pop a second window containing the scanner widgets.
    dtd_login_url: str = "https://www.warriortrading.com/dashboard/"
    dtd_api_host: str = "scan-prod.warriortrading.com"
    dtd_widgets: str = "Momo,Running_Up"
    dtd_five_pillars_widget: str = ""
    # Run the live observer headed by default so the user can manually click through the
    # WT member dashboard -> chatroom gates each session. Cookies persist so SSO is sticky.
    dtd_headless: bool = False

    # --- Funnel config ---
    candidate_cooldown_minutes: int = 10

    # --- Default filter rule thresholds (seeded on first boot) ---
    default_min_price: float = 1.50
    default_max_price: float = 20.00
    default_max_float: int = 20_000_000
    default_min_rel_vol_today: float = 3.0
    default_min_rel_vol_5min: float = 3.0
    default_min_rel_gain: float = 5.0
    default_require_news_within_minutes: int = 240

    # --- IBKR ---
    # Trading mode is asserted at connect time. paper accounts have a "DU" prefix; the
    # ibkr_check script and engine RiskGate hard-fail if PAPER_TRADING_ONLY is true and
    # the connected account does not start with "DU".
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 37
    ibkr_trading_mode: str = "paper"  # paper | live
    # Market data type sent via reqMarketDataType. Codes per IB API:
    # live=1, frozen=2, delayed=3, delayed_frozen=4. Paper accounts without market
    # data subscriptions can only stream `delayed` (free). Flip to `live` once a
    # real-time subscription is active in IBKR Client Portal.
    ibkr_market_data_type: str = "delayed"  # live | frozen | delayed | delayed_frozen
    live_trading_enabled: bool = False
    paper_trading_only: bool = True
    # Default system-wide stance: every signal needs a human Approve press before it
    # becomes an order. Engine runs can opt out per-run via the `autonomous` flag,
    # but only while paper_trading_only=True.
    manual_approval_required: bool = True
    # Target account routing. When the IBKR login has multiple linked accounts
    # (e.g. Individual + Trust), `ib.managedAccounts()` returns them all and
    # the engine MUST be told which one to use. If unset, the engine uses the
    # only-account-available rule: exactly one managed account -> use it;
    # multiple -> refuse to start.
    #
    # For paper: leave blank (paper login normally exposes one DU* account).
    # For live: MUST be set to the live account code that owns the trades
    # (e.g. U23755393 for the trust account).
    ibkr_target_account: str = ""

    # --- Risk guardrails (v1.5) ---
    max_daily_loss_usd: float = 200.0
    max_open_positions: int = 2
    max_trades_per_day: int = 10
    max_order_rate_per_min: int = 4
    min_seconds_before_open: int = 0

    # --- Auto-arm (Item 2) ---
    # Background worker that polls the candidates table for newly-passed
    # scanner alerts and spins up an engine for them. Defaults are
    # CONSERVATIVE; the global toggle is OFF until you explicitly enable.
    auto_arm_enabled: bool = False
    # Comma-separated list of scanner widget names that qualify for
    # auto-arm. "Momo" = WT Small Cap High of Day Momentum scanner.
    auto_arm_widgets: str = "Momo"
    auto_arm_strategy: str = "first_pullback_long"
    auto_arm_quantity: int = 100
    auto_arm_order_type: str = "LMT"  # LMT | MKT
    auto_arm_limit_offset_cents: float = 5.0
    auto_arm_enable_depth: bool = True
    auto_arm_enable_tape: bool = True
    # Fresh movers usually haven't accumulated 26+ 5m bars so the 5m
    # MACD context gate is OFF by default for auto-armed engines.
    auto_arm_require_5m_macd: bool = False
    auto_arm_autonomous: bool = False
    # Per-engine risk caps when auto-armed. Deliberately tighter than
    # manual arms because the engine wasn't human-vetted.
    auto_arm_max_daily_loss_usd: float = 50.0
    auto_arm_max_trades_per_run: int = 3
    auto_arm_max_position_value_usd: float = 5000.0
    auto_arm_max_position_qty: int = 25000
    # Trading window (ET, HH:MM). Outside this window auto-arm is
    # silently skipped.
    auto_arm_window_start_et: str = "04:00"
    auto_arm_window_end_et: str = "11:30"
    # Rate limits.
    auto_arm_max_per_day: int = 10
    auto_arm_max_per_hour: int = 3
    # If we auto-arm a symbol and it gets stopped (manually, or by
    # staleness, or by exit-trigger position close), don't auto-arm it
    # again for this many minutes.
    auto_arm_rearm_cooldown_minutes: int = 30
    # Staleness: if an auto-armed engine's underlying candidate has not
    # received a new scanner alert within this many minutes AND the
    # engine is not currently holding a position, the worker auto-stops
    # it to free a slot for the next mover.
    auto_arm_stale_after_minutes: int = 5
    # When fetching candidates for the arm path, only consider alerts
    # newer than this many seconds. Must be tighter than the staleness
    # threshold so we don't arm on a candidate that would be
    # immediately killed by the staleness watcher (the original
    # "armed and killed within 12s" bug). 90s gives a Ross-style fresh
    # mover signal while leaving the engine at least
    # `stale_after_minutes*60 - 90s` of staleness runway.
    auto_arm_lookback_seconds: float = 90.0
    # Grace period: the staleness watcher will not kill an auto-armed
    # engine younger than this many seconds. Belt-and-braces defence
    # against the same bug — even if we armed on a candidate that was
    # already near-stale, the engine gets a guaranteed minimum runtime
    # to bootstrap, evaluate, and decide.
    auto_arm_grace_period_seconds: float = 120.0
    # Polling cadence for the worker (DB hit every N seconds).
    auto_arm_poll_seconds: float = 2.0

    @property
    def playwright_profile_path(self) -> Path:
        p = Path(self.dtd_playwright_profile_dir)
        if not p.is_absolute():
            p = REPO_ROOT / p
        return p

    @property
    def widget_list(self) -> list[str]:
        widgets = [w.strip() for w in self.dtd_widgets.split(",") if w.strip()]
        if self.dtd_five_pillars_widget:
            widgets.append(self.dtd_five_pillars_widget.strip())
        return widgets

    @property
    def auto_arm_widget_list(self) -> list[str]:
        return [w.strip() for w in self.auto_arm_widgets.split(",") if w.strip()]

    @model_validator(mode="after")
    def _validate_safety_invariants(self) -> Settings:
        # Hard safety: incompatible combinations must not boot.
        if self.paper_trading_only and self.live_trading_enabled:
            raise ValueError(
                "PAPER_TRADING_ONLY=true and LIVE_TRADING_ENABLED=true are mutually exclusive."
            )
        if self.ibkr_trading_mode not in ("paper", "live"):
            raise ValueError(
                f"IBKR_TRADING_MODE must be 'paper' or 'live', got {self.ibkr_trading_mode!r}."
            )
        if self.ibkr_trading_mode == "live" and self.paper_trading_only:
            raise ValueError(
                "IBKR_TRADING_MODE=live conflicts with PAPER_TRADING_ONLY=true."
            )
        if self.ibkr_trading_mode == "live" and not self.live_trading_enabled:
            raise ValueError(
                "IBKR_TRADING_MODE=live requires LIVE_TRADING_ENABLED=true."
            )
        if self.ibkr_market_data_type not in ("live", "frozen", "delayed", "delayed_frozen"):
            raise ValueError(
                f"IBKR_MARKET_DATA_TYPE must be one of live|frozen|delayed|delayed_frozen, "
                f"got {self.ibkr_market_data_type!r}."
            )
        # Target account format / mode consistency.
        target = self.ibkr_target_account.strip()
        if target:
            if self.paper_trading_only and not target.startswith("DU"):
                raise ValueError(
                    f"IBKR_TARGET_ACCOUNT={target!r} is not a paper account "
                    f"(expected DU* prefix), but PAPER_TRADING_ONLY=true."
                )
            if not self.paper_trading_only and target.startswith("DU"):
                raise ValueError(
                    f"IBKR_TARGET_ACCOUNT={target!r} is a paper account, but "
                    f"PAPER_TRADING_ONLY=false. Set the live account code "
                    f"(e.g. U23755393) or flip PAPER_TRADING_ONLY=true."
                )
        return self

    @property
    def ibkr_market_data_type_code(self) -> int:
        return {"live": 1, "frozen": 2, "delayed": 3, "delayed_frozen": 4}[self.ibkr_market_data_type]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
