from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from src.safety import SafetyFlags


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    database_url: str = "sqlite:///./data/research.db"
    log_level: str = "INFO"

    okx_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    okx_symbols: str = "BTC-USDT,ETH-USDT,SOL-USDT"
    polymarket_crypto_keywords: str = "BTC,ETH,SOL,bitcoin,ethereum,solana,crypto,XRP,DOT"

    allow_real_trading: bool = False
    allow_private_keys: bool = False
    allow_withdrawals: bool = False
    allow_browser_automation: bool = False

    @property
    def symbols(self) -> list[str]:
        return [symbol.strip() for symbol in self.okx_symbols.split(",") if symbol.strip()]

    @property
    def crypto_keywords(self) -> list[str]:
        return [kw.strip() for kw in self.polymarket_crypto_keywords.split(",") if kw.strip()]

    @property
    def sqlite_path(self) -> str:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Phase 1 only supports sqlite:/// database URLs")
        return self.database_url.removeprefix(prefix)

    @property
    def safety_flags(self) -> SafetyFlags:
        return SafetyFlags(
            allow_real_trading=self.allow_real_trading,
            allow_private_keys=self.allow_private_keys,
            allow_withdrawals=self.allow_withdrawals,
            allow_browser_automation=self.allow_browser_automation,
        )


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    settings = Settings(
        app_env=os.getenv("APP_ENV", "local"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/research.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        okx_ws_url=os.getenv("OKX_WS_URL", "wss://ws.okx.com:8443/ws/v5/public"),
        polymarket_gamma_url=os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"),
        polymarket_clob_url=os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"),
        okx_symbols=os.getenv("OKX_SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT"),
        polymarket_crypto_keywords=os.getenv(
            "POLYMARKET_CRYPTO_KEYWORDS",
            "BTC,ETH,SOL,bitcoin,ethereum,solana,crypto,XRP,DOT",
        ),
        allow_real_trading=_env_bool("ALLOW_REAL_TRADING"),
        allow_private_keys=_env_bool("ALLOW_PRIVATE_KEYS"),
        allow_withdrawals=_env_bool("ALLOW_WITHDRAWALS"),
        allow_browser_automation=_env_bool("ALLOW_BROWSER_AUTOMATION"),
    )
    settings.safety_flags.enforce_phase_one()
    return settings
