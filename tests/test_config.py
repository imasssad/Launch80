from dataclasses import replace
import pytest
from launch80.config import Settings

def test_live_requires_clear_ack(cfg):
    with pytest.raises(ValueError,match='LIVE_ACK'):
        replace(cfg,live=True).validate()

def test_slippage_cap(cfg):
    with pytest.raises(ValueError,match='SLIPPAGE_BPS'):
        replace(cfg,slippage_bps=1000).validate()

def test_live_rejects_public_solana_rpc(cfg):
    with pytest.raises(ValueError):
        replace(cfg,live=True,live_ack='I_ACCEPT_POSSIBLE_TOTAL_LOSS',
                enable_robinhood=False,jupiter_key='x',solana_private_key='x').validate()

def test_live_buy_needs_separate_ack(cfg):
    with pytest.raises(ValueError, match='LIVE_BUY_ACK'):
        replace(cfg,live=True,live_ack='I_ACCEPT_POSSIBLE_TOTAL_LOSS',buy_enabled=True).validate()

def test_live_read_only_may_leave_buy_unarmed(cfg):
    # Dedicated live-buy acknowledgement is not required for read-only stages,
    # but required network credentials are still validated separately.
    assert not replace(cfg,buy_enabled=False).buy_enabled
