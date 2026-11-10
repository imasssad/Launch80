"""Regression tests for the issues found in the v1.1 import audit.

Each test pins a specific defect so it cannot silently return.
"""
from dataclasses import replace

import base58
import pytest

from launch80.config import SOLANA_MAINNET_GENESIS_HASH, Settings
from launch80.http import _retry_after_seconds


# --- Solana cluster guard ---------------------------------------------------

def test_expected_genesis_is_a_full_32_byte_hash():
    # The original constant was the 32-char CAIP-2 chain id prefix, which
    # decodes to 23 bytes and can never equal a getGenesisHash response.
    assert len(base58.b58decode(SOLANA_MAINNET_GENESIS_HASH)) == 32
    assert SOLANA_MAINNET_GENESIS_HASH == '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'


def test_caip2_chain_id_prefix_is_rejected_as_genesis_hash(cfg):
    caip2_prefix = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'
    assert SOLANA_MAINNET_GENESIS_HASH.startswith(caip2_prefix)
    with pytest.raises(ValueError, match='SOLANA_EXPECTED_GENESIS_HASH'):
        replace(cfg, solana_expected_genesis=caip2_prefix).validate()


def test_malformed_genesis_hash_rejected(cfg):
    with pytest.raises(ValueError, match='SOLANA_EXPECTED_GENESIS_HASH'):
        replace(cfg, solana_expected_genesis='not!base58!').validate()


def test_blank_env_falls_back_to_builtin_genesis_hash(monkeypatch, cfg):
    monkeypatch.setenv('SOLANA_EXPECTED_GENESIS_HASH', '')
    assert Settings.load().solana_expected_genesis == SOLANA_MAINNET_GENESIS_HASH


def test_genesis_hash_not_validated_when_solana_disabled(cfg):
    # Nothing reads it, so a bad value must not block a Robinhood-only deploy.
    replace(cfg, enable_solana=False, solana_expected_genesis='x').validate()


async def test_solana_validate_network_rejects_mismatched_cluster(cfg):
    from launch80.exchange import SolanaExchange
    from launch80.models import TradingError
    ex = SolanaExchange.__new__(SolanaExchange)
    ex.cfg = cfg

    async def wrong_cluster(method, params):
        assert method == 'getGenesisHash'
        return 'EtWTRABZaYq6iMfeYKouRu166VU2xqa1'  # devnet-shaped value

    ex._rpc = wrong_cluster
    with pytest.raises(TradingError, match='not Solana mainnet-beta'):
        await ex.validate_network()


# --- 0x settlement-target allowlist ----------------------------------------

def test_live_robinhood_requires_tx_target_allowlist(cfg):
    live = replace(cfg, live=True, live_ack='I_ACCEPT_POSSIBLE_TOTAL_LOSS',
                   live_buy_ack='I_ENABLE_REAL_PURCHASES',
                   enable_solana=False, zero_x_key='k', evm_private_key='k',
                   approved_allowance_spenders=('0xaaa',), approved_tx_targets=(),
                   robinhood_rpc='https://x.alchemy.com/v2/k')
    with pytest.raises(ValueError, match='ZEROX_APPROVED_TX_TARGETS'):
        live.validate()
    # Same config with the allowlist supplied is accepted.
    replace(live, approved_tx_targets=('0xbbb',)).validate()


def test_tx_targets_come_from_settings_not_the_environment(monkeypatch):
    # The adapter used to re-read os.environ, so Settings.validate() could pass
    # while the adapter used a different (or empty) allowlist.
    monkeypatch.setenv('ZEROX_APPROVED_TX_TARGETS', '0xDEADBEEF, 0xfeed')
    assert Settings.load().approved_tx_targets == ('0xdeadbeef', '0xfeed')


# --- Bounds that were previously unchecked ---------------------------------

@pytest.mark.parametrize('value', [0, -1])
def test_non_positive_exit_retry_delay_rejected(cfg, value):
    with pytest.raises(ValueError, match='MAX_EXIT_RETRY_SECONDS'):
        replace(cfg, max_exit_retry_seconds=value).validate()


@pytest.mark.parametrize('value', [0, -5])
def test_non_positive_feed_failure_budget_rejected(cfg, value):
    with pytest.raises(ValueError, match='MAX_CONSECUTIVE_FEED_FAILURES'):
        replace(cfg, max_consecutive_feed_failures=value).validate()


# --- Retry-After parsing ---------------------------------------------------

def test_retry_after_seconds_form():
    assert _retry_after_seconds('3') == 3.0
    assert _retry_after_seconds('  2.5 ') == 2.5


def test_retry_after_is_capped_and_never_negative():
    assert _retry_after_seconds('9999') == 8.0
    assert _retry_after_seconds('-5') == 0.0


def test_retry_after_http_date_does_not_raise():
    # RFC 9110 allows an HTTP-date; float() on it used to raise ValueError and
    # escape the retry loop, turning throttling into a hard feed failure.
    assert _retry_after_seconds('Wed, 21 Oct 2015 07:28:00 GMT') == 0.0
    assert _retry_after_seconds('garbage') == 0.0
    assert _retry_after_seconds(None) == 0.0


# --- Control-token comparison ----------------------------------------------

def _client(cfg):
    from fastapi.testclient import TestClient
    from launch80.server import create_app
    from launch80.storage import Store
    store = Store(cfg.db_path)
    return TestClient(create_app(store, cfg)), store


def test_non_ascii_authorization_header_is_401_not_500(cfg):
    # Starlette decodes request headers as latin-1, so a raw client can deliver
    # a header that becomes a non-ASCII str. hmac.compare_digest rejects
    # non-ASCII str operands with TypeError, which surfaced as a 500.
    client, store = _client(replace(cfg, control_token='k' * 24))
    try:
        raw = ('Bearer café-' + 'x' * 18).encode('latin-1')
        response = client.post('/pause', headers={'Authorization': raw})
        assert response.status_code == 401
        assert store.flag('paused') == '0'
    finally:
        store.close()


def test_correct_token_pauses_and_resumes(cfg):
    token = 'z' * 24
    client, store = _client(replace(cfg, control_token=token))
    try:
        assert client.post('/pause', headers={'Authorization': 'Bearer ' + token}).status_code == 200
        assert store.flag('paused') == '1'
        assert client.post('/resume', headers={'Authorization': 'Bearer ' + token}).status_code == 200
        assert store.flag('paused') == '0'
    finally:
        store.close()


def test_controls_disabled_without_token(cfg):
    client, store = _client(cfg)
    try:
        assert client.post('/pause').status_code == 403
    finally:
        store.close()


# --- Packaging consistency -------------------------------------------------

def test_version_matches_package_metadata():
    import importlib.metadata as md
    from launch80 import __version__
    assert __version__ == md.version('launch80')


def test_eth_account_is_a_declared_dependency():
    # exchange.py imports eth_account directly; it must not rely on web3's
    # transitive dependency.
    import importlib.metadata as md
    requires = md.requires('launch80') or []
    assert any(r.split(';')[0].strip().lower().startswith('eth-account') for r in requires)
