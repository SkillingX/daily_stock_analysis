"""Admission policy for new stock operations; historical reads remain unrestricted."""

from data_provider.base import _market_tag
from src.config import get_config


class MarketDisabledError(ValueError):
    """The deployment does not admit new operations for this market."""


def require_market(market: str, enabled: tuple[str, ...] | None = None) -> None:
    allowed = get_config().enabled_markets if enabled is None else enabled
    if market not in allowed:
        raise MarketDisabledError(f"市场 {market} 未启用；当前开放市场：{', '.join(allowed)}")


def require_stock_market(code: str, enabled: tuple[str, ...] | None = None) -> None:
    require_market(_market_tag(code), enabled)
