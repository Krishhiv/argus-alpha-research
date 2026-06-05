"""
Contract resolution for the paper trader.

Reads the same INSTRUMENT_MASTER_PATH that the collector uses (set in the
shared .env), resolves the current front-month security_id for each instrument
automatically. No manual updates needed on monthly expiry rolls.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("argus.contracts")


@dataclass(frozen=True)
class _Row:
    security_id: int
    underlying_symbol: str
    exchange: str
    segment: str
    instrument: str
    expiry_date: date
    lot_size: int


@dataclass(frozen=True)
class ResolvedContract:
    underlying: str
    security_id: int
    lot_size: int
    expiry_date: date


def _load_master(path: Path) -> list[_Row]:
    rows: list[_Row] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            try:
                rows.append(_Row(
                    security_id=int(raw["SECURITY_ID"]),
                    underlying_symbol=str(raw["UNDERLYING_SYMBOL"]).strip().upper(),
                    exchange=str(raw["EXCH_ID"]).strip().upper(),
                    segment=str(raw["SEGMENT"]).strip().upper(),
                    instrument=str(raw["INSTRUMENT"]).strip().upper(),
                    expiry_date=date.fromisoformat(str(raw["SM_EXPIRY_DATE"]).strip()),
                    lot_size=int(float(raw.get("LOT_SIZE", 0) or 0)),
                ))
            except (KeyError, ValueError):
                continue
    return rows


def resolve_contracts(
    underlying_symbols: list[str],
    *,
    instrument: str = "FUTSTK",
    exchange: str = "NSE",
    segment: str = "D",
    as_of: date | None = None,
) -> dict[str, ResolvedContract]:
    """
    Return {underlying_symbol: ResolvedContract} (security_id + lot_size +
    expiry) for the current front-month futures contract of each symbol.

    Reads INSTRUMENT_MASTER_PATH from the environment (the same .env the
    collector uses). Raises ValueError if any symbol cannot be resolved.
    """
    load_dotenv()
    master_path = Path(os.environ["INSTRUMENT_MASTER_PATH"]).expanduser()
    if not master_path.exists():
        raise FileNotFoundError(f"Instrument master not found: {master_path}")

    as_of_date = as_of or date.today()
    rows = _load_master(master_path)

    result: dict[str, ResolvedContract] = {}
    for sym in underlying_symbols:
        candidates = sorted(
            [
                r for r in rows
                if r.underlying_symbol == sym.upper()
                and r.exchange == exchange
                and r.segment == segment
                and r.instrument == instrument
                and r.expiry_date >= as_of_date
            ],
            key=lambda r: r.expiry_date,
        )
        if not candidates:
            raise ValueError(
                f"No active {exchange}/{instrument} contract for {sym} as of {as_of_date}. "
                f"Check that {master_path} is up to date."
            )
        s = candidates[0]  # nearest (current) expiry
        result[sym] = ResolvedContract(sym.upper(), s.security_id, s.lot_size, s.expiry_date)
        log.info("Resolved %s → security_id=%d lot=%d (expiry=%s)",
                 sym, s.security_id, s.lot_size, s.expiry_date)

    return result


def resolve_security_ids(
    underlying_symbols: list[str],
    *,
    instrument: str = "FUTSTK",
    exchange: str = "NSE",
    segment: str = "D",
    as_of: date | None = None,
) -> dict[str, int]:
    """Back-compat helper: {symbol: security_id} only."""
    contracts = resolve_contracts(
        underlying_symbols, instrument=instrument, exchange=exchange,
        segment=segment, as_of=as_of,
    )
    return {sym: c.security_id for sym, c in contracts.items()}
