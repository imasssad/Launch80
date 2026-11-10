"""Independent on-chain fill verification for `launch80 reconcile` in live mode.

`--verify-onchain` used to be a pure human attestation. In live mode the RPC
credentials are available anyway, so a txid-bearing reconciliation
(buy-filled / sell-filled) is now additionally checked against the chain:
the transaction must exist and must not have failed. Anything that cannot be
confirmed — unknown txid, reverted receipt, RPC unreachable — refuses the
reconciliation (fail closed); the operator retries once the RPC answers.

This confirms the transaction LANDED AND SUCCEEDED. It does not decode
amounts, so the operator-supplied --spent-atomic/--received-atomic values
remain an attestation that must come from independent explorer inspection.
NEEDS LIVE VALIDATION against funded wallets on both chains.
"""
from __future__ import annotations
import asyncio
import logging

from .config import Settings
from .http import Api

LOG = logging.getLogger('launch80.verify')


async def confirmed_onchain(cfg: Settings, chain: str, txid: str, exchange=None) -> bool:
    """True only when the transaction is found on-chain and did not fail."""
    if not txid:
        return False
    api = None
    try:
        if exchange is None:
            api = Api()
            if chain == 'robinhood':
                from .exchange import RobinhoodExchange
                exchange = RobinhoodExchange(cfg, api)
            elif chain == 'solana':
                from .exchange import SolanaExchange
                exchange = SolanaExchange(cfg, api)
            else:
                return False
        if chain == 'robinhood':
            try:
                receipt = await asyncio.to_thread(exchange.w3.eth.get_transaction_receipt, txid)
            except Exception as exc:
                LOG.warning('EVM receipt lookup failed: %s', exc.__class__.__name__)
                return False
            return receipt is not None and int(receipt['status']) == 1
        if chain == 'solana':
            try:
                result = await exchange._rpc('getTransaction', [txid, {
                    'encoding': 'json', 'commitment': 'finalized',
                    'maxSupportedTransactionVersion': 0}])
            except Exception as exc:
                LOG.warning('Solana transaction lookup failed: %s', exc.__class__.__name__)
                return False
            return bool(result) and ((result.get('meta') or {}).get('err') is None)
        return False
    finally:
        if api is not None:
            await api.close()
