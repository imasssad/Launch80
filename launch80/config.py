"""Configuration is strict; live mode requires multiple independent opt-ins."""
import os
from dataclasses import dataclass
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

def _b(s: str, default: bool = False) -> bool:
    raw = os.getenv(s, str(default)).strip().lower()
    if raw not in ('true', 'false', '1', '0', 'yes', 'no'):
        raise ValueError(f'{s} must be boolean')
    return raw in ('true', '1', 'yes')

def _d(s: str, default: str) -> Decimal:
    value = Decimal(os.getenv(s, default))
    if not value.is_finite():
        raise ValueError(f'{s} must be finite')
    return value

@dataclass(frozen=True)
class Settings:
    live: bool
    live_ack: str
    live_buy_ack: str
    buy_enabled: bool
    enable_robinhood: bool
    enable_solana: bool
    db_path: str
    min_liquidity_usd: Decimal
    buy_usd: Decimal
    max_buy_liquidity_fraction: Decimal
    max_open_positions: int
    max_total_exposure_usd: Decimal
    max_daily_realized_loss_usd: Decimal
    max_price_impact_bps: int
    max_roundtrip_cost_bps: int
    slippage_bps: int
    hold_seconds: int
    min_age_seconds: int
    max_age_seconds: int
    poll_seconds: int
    page_count: int
    max_buy_attempts_per_hour: int
    max_consecutive_feed_failures: int
    max_exit_retry_seconds: int
    control_token: str
    robinhood_rpc: str
    solana_rpc: str
    zero_x_key: str
    jupiter_key: str
    evm_private_key: str
    solana_private_key: str
    approved_allowance_spenders: tuple[str, ...]
    robinhood_usdg: str
    solana_usdc: str
    require_robinhood_simulation: bool
    garp_enrich: bool
    garp_require_launchpad: bool
    max_exit_attempts: int
    min_exit_profit_bps: int
    exit_cost_buffer_bps: int
    profit_recheck_seconds: int
    underwater_alert_seconds: int
    underwater_pause_bps: int

    @classmethod
    def load(cls):
        result = cls(
            live=_b('LIVE'),
            live_ack=os.getenv('LIVE_ACK', ''),
            live_buy_ack=os.getenv('LIVE_BUY_ACK', ''),
            buy_enabled=_b('BUY_ENABLED', False),
            enable_robinhood=_b('ENABLE_ROBINHOOD', True),
            enable_solana=_b('ENABLE_SOLANA', True),
            db_path=os.getenv('DB_PATH', './data/launch80.sqlite'),
            min_liquidity_usd=_d('MIN_LIQUIDITY_USD', '5000'),
            buy_usd=_d('BUY_USD', '5'),
            max_buy_liquidity_fraction=_d('MAX_LIQUIDITY_FRACTION', '0.001'),
            max_open_positions=int(os.getenv('MAX_OPEN_POSITIONS', '2')),
            max_total_exposure_usd=_d('MAX_TOTAL_EXPOSURE_USD', '20'),
            max_daily_realized_loss_usd=_d('MAX_DAILY_REALIZED_LOSS_USD', '20'),
            max_price_impact_bps=int(os.getenv('MAX_PRICE_IMPACT_BPS', '150')),
            max_roundtrip_cost_bps=int(os.getenv('MAX_ROUNDTRIP_COST_BPS', '600')),
            slippage_bps=int(os.getenv('SLIPPAGE_BPS', '100')),
            hold_seconds=int(os.getenv('HOLD_SECONDS', '80')),
            min_age_seconds=int(os.getenv('MIN_POOL_AGE_SECONDS', '12')),
            max_age_seconds=int(os.getenv('MAX_POOL_AGE_SECONDS', '150')),
            poll_seconds=int(os.getenv('POLL_SECONDS', '30')),
            page_count=int(os.getenv('PAGE_COUNT', '2')),
            max_buy_attempts_per_hour=int(os.getenv('MAX_BUY_ATTEMPTS_PER_HOUR', '10')),
            max_consecutive_feed_failures=int(os.getenv('MAX_CONSECUTIVE_FEED_FAILURES', '5')),
            max_exit_retry_seconds=int(os.getenv('MAX_EXIT_RETRY_SECONDS', '45')),
            control_token=os.getenv('CONTROL_TOKEN', ''),
            robinhood_rpc=os.getenv('ROBINHOOD_RPC_URL', 'https://rpc.mainnet.chain.robinhood.com'),
            solana_rpc=os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com'),
            zero_x_key=os.getenv('ZEROX_API_KEY', ''),
            jupiter_key=os.getenv('JUPITER_API_KEY', ''),
            evm_private_key=os.getenv('EVM_PRIVATE_KEY', ''),
            solana_private_key=os.getenv('SOLANA_PRIVATE_KEY_B58', ''),
            approved_allowance_spenders=tuple(x.strip().lower() for x in os.getenv('ZEROX_APPROVED_SPENDERS', '').split(',') if x.strip()),
            robinhood_usdg='0x5fc5360d0400a0fd4f2af552add042d716f1d168',
            solana_usdc='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
            require_robinhood_simulation=_b('REQUIRE_ROBINHOOD_SIMULATION', True),
            garp_enrich=_b('GARP_ENRICH', True),
            garp_require_launchpad=_b('GARP_REQUIRE_LAUNCHPAD', False),
            max_exit_attempts=int(os.getenv('MAX_EXIT_ATTEMPTS','8')),
            min_exit_profit_bps=int(os.getenv('MIN_EXIT_PROFIT_BPS','100')),
            exit_cost_buffer_bps=int(os.getenv('EXIT_COST_BUFFER_BPS','50')),
            profit_recheck_seconds=int(os.getenv('PROFIT_RECHECK_SECONDS','12')),
            underwater_alert_seconds=int(os.getenv('UNDERWATER_ALERT_SECONDS','900')),
            underwater_pause_bps=int(os.getenv('UNDERWATER_PAUSE_BPS','3500')),
        )
        result.validate()
        return result

    def validate(self):
        if self.min_liquidity_usd < 5000:
            raise ValueError('MIN_LIQUIDITY_USD must be at least 5000')
        if not self.enable_robinhood and not self.enable_solana:
            raise ValueError('Enable at least one chain')
        if not 0 < self.buy_usd <= 100000:
            raise ValueError('BUY_USD invalid')
        if not 0 < self.max_buy_liquidity_fraction <= Decimal('0.05'):
            raise ValueError('MAX_LIQUIDITY_FRACTION invalid')
        if not 0 < self.max_open_positions <= 100:
            raise ValueError('MAX_OPEN_POSITIONS invalid')
        if not 0 < self.max_total_exposure_usd:
            raise ValueError('MAX_TOTAL_EXPOSURE_USD invalid')
        if not 0 < self.max_daily_realized_loss_usd:
            raise ValueError('MAX_DAILY_REALIZED_LOSS_USD invalid')
        if not 0 <= self.max_price_impact_bps <= 500:
            raise ValueError('MAX_PRICE_IMPACT_BPS must be 0..500')
        if not 0 <= self.max_roundtrip_cost_bps <= 2000:
            raise ValueError('MAX_ROUNDTRIP_COST_BPS must be 0..2000')
        if not 0 < self.slippage_bps <= 300:
            raise ValueError('SLIPPAGE_BPS must be 1..300')
        if not 1 <= self.hold_seconds <= 86400:
            raise ValueError('HOLD_SECONDS invalid')
        if not 0 <= self.min_age_seconds < self.max_age_seconds <= 3600:
            raise ValueError('Pool age limits invalid')
        if not 5 <= self.poll_seconds <= 600:
            raise ValueError('POLL_SECONDS invalid')
        if not 1 <= self.page_count <= 5:
            raise ValueError('PAGE_COUNT invalid')
        if not 1 <= self.max_buy_attempts_per_hour <= 1000:
            raise ValueError('MAX_BUY_ATTEMPTS_PER_HOUR invalid')
        if not 1 <= self.max_exit_attempts <= 100:
            raise ValueError('MAX_EXIT_ATTEMPTS invalid')
        if not 0 <= self.min_exit_profit_bps <= 10000:
            raise ValueError('MIN_EXIT_PROFIT_BPS must be 0..10000')
        if not 0 <= self.exit_cost_buffer_bps <= 5000:
            raise ValueError('EXIT_COST_BUFFER_BPS must be 0..5000')
        if not 3 <= self.profit_recheck_seconds <= 3600:
            raise ValueError('PROFIT_RECHECK_SECONDS must be 3..3600')
        if not 30 <= self.underwater_alert_seconds <= 604800:
            raise ValueError('UNDERWATER_ALERT_SECONDS must be 30..604800')
        if not 1 <= self.underwater_pause_bps <= 10000:
            raise ValueError('UNDERWATER_PAUSE_BPS must be 1..10000')
        if self.garp_require_launchpad and not self.garp_enrich:
            raise ValueError('GARP_REQUIRE_LAUNCHPAD needs GARP_ENRICH=true')
        if self.live:
            if self.live_ack != 'I_ACCEPT_POSSIBLE_TOTAL_LOSS':
                raise ValueError('Live mode requires LIVE_ACK=I_ACCEPT_POSSIBLE_TOTAL_LOSS')
            if self.buy_enabled and self.live_buy_ack != 'I_ENABLE_REAL_PURCHASES':
                raise ValueError('Live buying requires LIVE_BUY_ACK=I_ENABLE_REAL_PURCHASES')
            if self.enable_robinhood and not (self.zero_x_key and self.evm_private_key and self.approved_allowance_spenders):
                raise ValueError('Live Robinhood requires ZEROX_API_KEY, EVM_PRIVATE_KEY, ZEROX_APPROVED_SPENDERS')
            if self.enable_solana and not (self.jupiter_key and self.solana_private_key):
                raise ValueError('Live Solana requires JUPITER_API_KEY, SOLANA_PRIVATE_KEY_B58')
            if self.enable_robinhood and 'alchemy' not in self.robinhood_rpc and 'chainstack' not in self.robinhood_rpc and 'quiknode' not in self.robinhood_rpc:
                raise ValueError('Use a production Robinhood RPC provider, not rate-limited public RPC')
            if self.enable_solana and self.solana_rpc == 'https://api.mainnet-beta.solana.com':
                raise ValueError('Use a production Solana RPC, not public mainnet RPC')
        if self.control_token and len(self.control_token) < 24:
            raise ValueError('CONTROL_TOKEN should be >=24 chars when present')
