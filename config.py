"""Configuration for the EastMoney global market monitor.

Values come from .env (next to this file) and can be overridden by real
environment variables, which is what the deployment scripts use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"


def parse_env_file(path: Path) -> dict[str, str]:
    """Read a flat KEY=VALUE .env file. Later keys win, comments are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class Settings:
    # --- dmdaili (dynamic proxy) ---
    proxy_apikey: str = ""
    proxy_pwd: str = ""
    proxy_enabled: bool = True
    proxy_api: str = "http://api.dmdaili.com/dmgetip.asp"
    proxy_batch: int = 20           # IPs requested per extraction
    proxy_target: int = 20          # keep this many fresh IPs alive
    proxy_min_gap: float = 1.0      # seconds between extractions (provider allows ~10/s)
    proxy_ttl_margin: int = 3       # stop using an IP this many seconds before it expires
    proxy_country: str = "all"
    proxy_scheme: str = "http"      # scheme used when dialling the proxy

    # --- EastMoney ---
    api_hosts: tuple[str, ...] = ("push2.eastmoney.com", "push2his.eastmoney.com")
    page_size: int = 100
    max_pages: int = 80
    request_timeout: float = 12.0
    max_retries: int = 3
    rate_limit: float = 10.0        # requests per second, global
    stock_budget: float = 1.0       # seconds an interactive stock request may wait

    # --- collector ---
    workers: int = 6
    cycle_sleep: float = 2.0
    idle_sleep: float = 30.0        # when every market is closed
    history_enabled: bool = True
    retention_hours: int = 72
    kline_refresh_hours: int = 6
    signal_retention_days: int = 7
    intel_interval: int = 600
    intel_bodies_per_cycle: int = 8
    signals_enabled: bool = True

    # --- storage / server ---
    db_path: Path = field(default_factory=lambda: DATA_DIR / "eastmoney.db")
    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "Settings":
        env = parse_env_file(BASE_DIR / ".env")

        def get(key: str, default: str | None = None) -> str | None:
            if key in os.environ:
                return os.environ[key]
            return env.get(key, default)

        db = get("EM_DB") or str(DATA_DIR / "eastmoney.db")
        s = cls(
            proxy_apikey=get("dmdaili_apikey") or get("EM_PROXY_APIKEY", "") or "",
            proxy_pwd=get("dmdaili_pwd") or get("EM_PROXY_PWD", "") or "",
            proxy_enabled=_bool(get("EM_USE_PROXY"), True),
            proxy_api=get("EM_PROXY_API", "http://api.dmdaili.com/dmgetip.asp") or "",
            proxy_batch=_int(get("EM_PROXY_BATCH"), 20),
            proxy_target=_int(get("EM_PROXY_TARGET"), 20),
            proxy_min_gap=_float(get("EM_PROXY_MIN_GAP"), 1.0),
            proxy_ttl_margin=_int(get("EM_PROXY_TTL_MARGIN"), 3),
            proxy_country=get("EM_PROXY_AREA", "all") or "all",
            page_size=_int(get("EM_PAGE_SIZE"), 100),
            max_pages=_int(get("EM_MAX_PAGES"), 80),
            request_timeout=_float(get("EM_TIMEOUT"), 12.0),
            max_retries=_int(get("EM_MAX_RETRIES"), 3),
            rate_limit=_float(get("EM_RATE"), 10.0),
            stock_budget=_float(get("EM_STOCK_BUDGET"), 1.0),
            workers=_int(get("EM_WORKERS"), 6),
            cycle_sleep=_float(get("EM_CYCLE_SLEEP"), 2.0),
            idle_sleep=_float(get("EM_IDLE_SLEEP"), 30.0),
            history_enabled=_bool(get("EM_HISTORY"), True),
            retention_hours=_int(get("EM_RETENTION_HOURS"), 72),
            kline_refresh_hours=_int(get("EM_KLINE_HOURS"), 6),
            signal_retention_days=_int(get("EM_SIGNAL_DAYS"), 7),
            intel_interval=_int(get("EM_INTEL_INTERVAL"), 600),
            intel_bodies_per_cycle=_int(get("EM_INTEL_BODIES"), 8),
            signals_enabled=_bool(get("EM_SIGNALS"), True),
            db_path=Path(db) if os.path.isabs(db) else BASE_DIR / db,
            host=get("EM_HOST", "127.0.0.1") or "127.0.0.1",
            port=_int(get("EM_PORT"), 8787),
            log_level=(get("EM_LOG_LEVEL", "INFO") or "INFO").upper(),
        )
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        return s

    @property
    def proxy_configured(self) -> bool:
        return bool(self.proxy_apikey and self.proxy_pwd)


SETTINGS = Settings.load()
