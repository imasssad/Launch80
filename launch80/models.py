"""Domain models. All monetary sizes use Decimal or atomic integer units."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Chain = Literal['robinhood', 'solana']

@dataclass(frozen=True)
class Pool:
    chain: Chain
    pool: str
    mint: str
    symbol: str
    dex: str
    liquidity_usd: Decimal
    created_at: float

    @property
    def key(self) -> str:
        return f'{self.chain}:{self.pool.lower()}'

@dataclass(frozen=True)
class Quote:
    out_units: int
    price_impact_bps: int | None = None
    # Conservative, execution-enforced minimum output after configured slippage.
    min_out_units: int | None = None

@dataclass(frozen=True)
class Fill:
    txid: str
    spent_units: int
    received_units: int
    # network gas, in native smallest units (not included in USD trading PnL)
    gas_units: int = 0

class TradingError(Exception):
    pass

class RiskRejection(TradingError):
    pass

class UncertainExecution(TradingError):
    """Request possibly broadcast; do NOT retry without reconciliation."""
    pass
