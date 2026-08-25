"""ALGODEV-31: resolve a strategy's trading symbol against the verified
`broker_asset_symbols` table (webapp/models.py::BrokerAssetSymbol) instead
of guessing from a hardcoded candidate list.

That table starts empty by design and is filled in only as each broker is
actually connected to and a symbol verified against its real symbol list
(ProtoOASymbolsListReq or equivalent) -- see BrokerAssetSymbol's own
docstring and webapp.cli's `verify-symbol` command, which is how a row gets
there. This module is the read side: given a (broker, asset, platform), it
returns a candidate list for the caller to try against the broker's LIVE
symbol list (e.g. bot/ctrader_s007.py::run_live_cycle's own
`_load_symbols()` match) -- resolving here narrows/orders that list, it
never replaces the live check, since a broker can still rename or delist a
symbol after it was verified.

Three outcomes, and only three -- never a silent guess:
  1. verified row exists -> [row.broker_symbol], exactly one candidate.
  2. no row, but a fallback list was given -> that list, unchanged, logged
     at WARNING (this is the pre-ALGODEV-31 behavior, kept working but no
     longer silent -- see s007_config.py's SYMBOL_CANDIDATES).
  3. no row and no fallback -> SystemExit. Wrong symbol on a funded account
     is real money in the wrong place; refusing to trade beats guessing.
"""
from __future__ import annotations

import logging

from webapp.models import Asset, BrokerAssetSymbol

log = logging.getLogger(__name__)


def resolve_symbol(*, session, broker_id: int, asset_symbol: str, platform: str,
                    fallback_candidates: list[str] | None = None) -> list[str]:
    """`session`: an open SQLAlchemy session (caller-owned -- this function
    only reads, never commits/closes, matching webapp/runner.py's workers'
    own session-ownership convention)."""
    asset = session.query(Asset).filter_by(symbol=asset_symbol).one_or_none()
    row = None
    if asset is not None:
        row = session.query(BrokerAssetSymbol).filter_by(
            broker_id=broker_id, asset_id=asset.id, platform=platform).one_or_none()

    if row is not None:
        log.info("symbol_resolver: broker_id=%s asset=%s platform=%s -> verified '%s' "
                 "(broker_asset_symbols id=%s, verified_at=%s)",
                 broker_id, asset_symbol, platform, row.broker_symbol, row.id, row.verified_at)
        return [row.broker_symbol]

    if fallback_candidates:
        log.warning("symbol_resolver: no verified broker_asset_symbols row for broker_id=%s "
                    "asset=%s platform=%s -- falling back to candidate list %s. Once the live "
                    "symbol is confirmed, run 'python -m webapp.cli verify-symbol' to stop this "
                    "warning and remove the guesswork.",
                    broker_id, asset_symbol, platform, fallback_candidates)
        return list(fallback_candidates)

    raise SystemExit(
        f"symbol_resolver: no verified broker_asset_symbols row and no fallback candidates "
        f"for broker_id={broker_id} asset={asset_symbol} platform={platform} -- refusing to "
        f"guess a symbol for a live trading cycle")
