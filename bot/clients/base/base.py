"""Shared base for every broker API client under `bot/clients/`."""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseClient(ABC):
    """Common base for every broker API client.

    Every client is constructed from a single credentials dict so all
    subclasses share one contract. The expected keys are broker-specific;
    a cTrader client, for example, reads client_id / client_secret /
    access_token / account_id / host. The abstract methods below are the
    shared capability contract every client must provide; the exact
    argument and return shapes are refined per concrete client, since
    each venue names and models its own accounts, symbols and positions.
    """

    def __init__(self, creds: dict):
        self.creds = creds

    # ---------- shared capability contract ----------
    # Deliberately NOT abstract: market-data history (`get_trendbars` and
    # friends -- data clients and execution clients diverge here), symbol
    # discovery (`list_symbols` / `resolve_symbol`), trade history
    # (`get_deals`), account discovery (`list_accounts`), limit orders and
    # order amend/cancel. A pure-execution client legitimately lacks them,
    # so requiring them would break otherwise valid subclasses.

    @abstractmethod
    def check(self) -> dict:
        """Connectivity and auth probe.

        Returns a broker-specific status dict that at minimum carries the
        account balance/equity, so a caller can assert the client is live
        and funded before trading.
        """

    @abstractmethod
    def get_balance(self) -> float:
        """Account balance / wallet equity in the settlement currency."""

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        """Currently open positions, one dict per position.

        Keys are broker-specific but must include a stable position
        identifier and the side of the position.
        """

    @abstractmethod
    def get_symbol_details(self, symbol: str) -> dict:
        """Contract/instrument metadata for `symbol`.

        Carries at least price precision, size step and min/max size, so
        the caller can round prices and sizes the venue will accept.
        """

    @abstractmethod
    def place_market_order(self, symbol: str, side: str, **params) -> dict:
        """Open or add to a position at market.

        `side` is "buy" or "sell". Size and any protection (SL/TP) are
        passed broker-specifically as keyword params. Returns a
        broker-specific result dict.
        """

    @abstractmethod
    def close_position(self, position_id, volume) -> dict:
        """Flatten an open position, fully or partially.

        Symbol-based venues (e.g. Bybit, which closes by symbol and side)
        substitute their own identifier shape for `position_id` / `volume`.
        """
