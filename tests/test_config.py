from src.config import Settings


def test_symbols_are_parsed_from_comma_separated_env_value():
    settings = Settings(okx_symbols="BTC-USDT, ETH-USDT,,SOL-USDT")

    assert settings.symbols == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


def test_sqlite_path_requires_sqlite_url():
    settings = Settings(database_url="sqlite:///./data/research.db")

    assert settings.sqlite_path == "./data/research.db"

