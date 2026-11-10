"""Chain-specific quote and execution adapters.

Both live implementations require the caller to persist a BUYING/SELLING intent
BEFORE calling trade(). If a send is uncertain, it must never be auto-repeated.
"""
from __future__ import annotations
import asyncio
import base64
import time
import uuid
from decimal import Decimal
from typing import Literal

from .config import Settings
from .http import Api
from .models import Fill, Pool, Quote, RiskRejection, TradingError, UncertainExecution

Side = Literal['buy','sell']


class Exchange:
    chain: str
    async def quote(self, pool: Pool, side: Side, amount: int) -> Quote:
        raise NotImplementedError
    async def trade(self, pool: Pool, side: Side, amount: int, min_output_units: int | None = None) -> Fill:
        raise NotImplementedError
    async def token_balance(self, pool: Pool) -> int:
        raise NotImplementedError
    async def token_safety(self, pool: Pool) -> None:
        raise NotImplementedError


class PaperExchange(Exchange):
    """Virtual swaps against a live GeckoTerminal spot price and conservative CPMM model.

    Synthetic token denomination is 1e9 units/token. Not a fill simulation of
    the underlying venue; it is a *rough* paper execution model, never a PnL guarantee.
    """
    def __init__(self, chain: str, api: Api):
        self.chain, self.api = chain, api
        self.fee_bps = 150  # combined protocol/aggregator approximation per side

    async def _spot(self, pool: Pool):
        data = await self.api.get(f'https://api.geckoterminal.com/api/v2/networks/{pool.chain}/pools/{pool.pool}')
        a = data['data']['attributes']
        price = Decimal(str(a['base_token_price_usd']))
        liquidity = Decimal(str(a['reserve_in_usd']))
        if not (price.is_finite() and liquidity.is_finite() and price > 0 and liquidity > 0):
            raise TradingError('Paper spot/liquidity not available')
        return price, liquidity

    async def quote(self, pool: Pool, side: Side, amount: int) -> Quote:
        price, liquidity = await self._spot(pool)
        if amount <= 0:
            raise TradingError('Zero input')
        if side == 'buy':
            input_usd = Decimal(amount) / 1_000_000
        else:
            input_usd = (Decimal(amount)/1_000_000_000)*price
        # Conservative spread + constant-product AMM impact against roughly half TVL.
        fee_factor = Decimal(1) - Decimal(self.fee_bps) / 10000
        x = input_usd * fee_factor
        out_usd = x / (1 + x / (liquidity/2))
        output_units = int((out_usd / price * 1_000_000_000) if side == 'buy' else (out_usd*1_000_000))
        impact = int((1 - out_usd / x)*10000) if x > 0 else 10000
        if output_units <= 0:
            raise TradingError('Paper quote rounds to zero')
        return Quote(output_units, impact, output_units*(10000-100)//10000)

    async def trade(self, pool: Pool, side: Side, amount: int, min_output_units: int | None = None) -> Fill:
        try:
            quote = await self.quote(pool,side,amount)
        except TradingError:
            raise
        except Exception as exc:
            # Paper execution signs and broadcasts NOTHING, so a failed
            # simulation is a CONFIRMED non-execution, never an uncertain one.
            # Letting a transport error (e.g. a 429 from the price feed) escape
            # as an unexpected exception would make the engine quarantine the
            # position as RECOVERY_BUY and halt trading for manual onchain
            # reconciliation of a trade that could not possibly have happened.
            # Mapping it to TradingError keeps that quarantine for real sends.
            raise TradingError(f'Paper fill unavailable: {exc.__class__.__name__}') from exc
        if min_output_units is not None and (quote.min_out_units is None
                                             or quote.min_out_units < min_output_units):
            raise RiskRejection('Paper exit became unprofitable before fill')
        # Mark-to-live-spot at execution time, with fee and market impact above.
        return Fill(f'PAPER-{uuid.uuid4().hex[:18]}', amount, quote.out_units, 0)

    async def token_balance(self, pool: Pool) -> int:
        # Paper positions live in SQLite, not in a wallet.
        return 0

    async def token_safety(self, pool: Pool) -> None:
        # Paper risk-scanner can be explicitly enabled when moving to live.
        return None


ERC20_ABI = [
    {'constant': True,'inputs':[{'name':'owner','type':'address'}],
     'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'constant': True,'inputs':[{'name':'owner','type':'address'},{'name':'spender','type':'address'}],
     'name':'allowance','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'constant': False,'inputs':[{'name':'spender','type':'address'},{'name':'value','type':'uint256'}],
     'name':'approve','outputs':[{'name':'','type':'bool'}],'type':'function'},
    {'constant': True,'inputs':[],'name':'decimals',
     'outputs':[{'name':'','type':'uint8'}],'type':'function'},
]


class RobinhoodExchange(Exchange):
    chain = 'robinhood'

    def __init__(self, cfg: Settings, api: Api):
        from web3 import Web3
        from eth_account import Account
        self.cfg, self.api = cfg, api
        self.w3 = Web3(Web3.HTTPProvider(cfg.robinhood_rpc, request_kwargs={'timeout': 15}))
        self.account = Account.from_key(cfg.evm_private_key)
        self.wallet = self.account.address
        self.lock = asyncio.Lock()
        self.usdg = Web3.to_checksum_address(cfg.robinhood_usdg)
        self.allowed_spenders = set(a.lower() for a in cfg.approved_allowance_spenders)
        # Sourced from validated Settings, not re-read from the process
        # environment, so the live allowlist cannot diverge from the config
        # that Settings.validate() already checked.
        self.allowed_targets = set(a.lower() for a in cfg.approved_tx_targets)
        if not self.allowed_targets:
            raise ValueError('Live Robinhood needs ZEROX_APPROVED_TX_TARGETS verified from official 0x settlement addresses')

    async def validate_network(self):
        chain = await asyncio.to_thread(lambda: self.w3.eth.chain_id)
        if chain != 4663:
            raise TradingError(f'Unexpected Robinhood Chain ID {chain}, expected 4663')
        code = await asyncio.to_thread(self.w3.eth.get_code, self.usdg)
        if len(code) == 0:
            raise TradingError('USDG token contract missing from Robinhood RPC')
        decimals = await asyncio.to_thread(lambda: self._contract(self.cfg.robinhood_usdg).functions.decimals().call())
        if decimals != 6:
            raise TradingError(f'Robinhood quote token has {decimals} decimals; expected 6')

    def _asset(self, pool: Pool, side: Side):
        return (self.cfg.robinhood_usdg, pool.mint) if side == 'buy' else (pool.mint, self.cfg.robinhood_usdg)

    async def _zero_x(self, pool: Pool, side: Side, amount: int, firm: bool):
        if amount < 1:
            raise TradingError('Amount too small')
        src, dst = self._asset(pool, side)
        url = f'https://api.0x.org/swap/allowance-holder/{"quote" if firm else "price"}'
        return await self.api.get(url, params={
            'chainId': 4663, 'sellToken': src, 'buyToken': dst,
            'sellAmount': str(amount), 'taker': self.wallet,
            'slippageBps': self.cfg.slippage_bps,
        }, headers={'0x-api-key':self.cfg.zero_x_key,'0x-version':'v2'}, retries=1)

    async def quote(self, pool: Pool, side: Side, amount: int) -> Quote:
        data = await self._zero_x(pool,side,amount,False)
        if not data.get('liquidityAvailable',False):
            raise TradingError('0x no executable liquidity')
        if int(data.get('buyAmount',0)) <= 0:
            raise TradingError('0x returned no output amount')
        minimum = data.get('minBuyAmount')
        return Quote(int(data['buyAmount']), self._parse_impact(data),
                     int(minimum) if minimum is not None else None)

    @staticmethod
    def _parse_impact(data):
        # No standard 0x impact field across all liquidity sources. Missing =>
        # fail-safe with roundtrip quote and max portion of pool in the engine.
        x = data.get('estimatedPriceImpact')
        if x is None:
            return None
        try:
            d = Decimal(str(x))
            return int(d*10000) if d < 1 else int(d*100)
        except Exception:
            return None

    def _contract(self, token: str):
        from web3 import Web3
        return self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

    def _balance(self, token: str):
        return self._contract(token).functions.balanceOf(self.wallet).call()

    async def token_balance(self, pool: Pool) -> int:
        return await asyncio.to_thread(self._balance, pool.mint)

    async def token_safety(self, pool: Pool) -> None:
        if not self.cfg.require_robinhood_simulation:
            return
        data = await self.api.get(f'https://api.swarm0x.sh/v1/check/{pool.mint}', retries=1)
        verdict = data.get('verdict') or {}
        trade = data.get('trade') or {}
        if data.get('chainId') != 4663 or verdict.get('code') != 'ok':
            raise RiskRejection('Robinhood round-trip safety simulation did not pass')
        if trade.get('buyReverts') or trade.get('sellReverts'):
            raise RiskRejection('Simulated buy or sell reverts')
        loss = trade.get('roundTripLossBps')
        if loss is None or Decimal(str(loss)) > self.cfg.max_roundtrip_cost_bps:
            raise RiskRejection('Simulation cost exceeds limit or unavailable')
        checked = data.get('checkedAt')
        if checked:
            from datetime import datetime
            stamp = datetime.fromisoformat(checked.replace('Z','+00:00')).timestamp()
            if time.time()-stamp > 180:
                raise RiskRejection('Safety simulation stale')

    def _send(self, to: str, data: str, value: int, gas: int | None = None) -> tuple[str,int]:
        from web3 import Web3
        if self.w3.eth.get_balance(self.wallet) < int(Decimal('0.001') * Decimal(10**18)):
            raise RiskRejection('EVM wallet gas balance < 0.001 ETH reserve')
        nonce = self.w3.eth.get_transaction_count(self.wallet, 'pending')
        tx = {'chainId':4663, 'from':self.wallet, 'to':Web3.to_checksum_address(to),
              'nonce':nonce,'value':value, 'data':data,
              'gasPrice': self.w3.eth.gas_price}
        tx['gas'] = gas or min(1_600_000, int(self.w3.eth.estimate_gas(tx)*1.25))
        if tx['gas'] > 1_600_000:
            raise RiskRejection('Excessive EVM gas estimate')
        gas_native = Decimal(tx['gas']*tx['gasPrice']) / Decimal(10**18)
        if gas_native > Decimal('0.0007'):
            raise RiskRejection('Gas estimate exceeds 0.0007 ETH per transaction')
        # eth_call is an additional fail-fast check, not a future sell guarantee.
        self.w3.eth.call({k:v for k,v in tx.items() if k in ('from','to','value','data','gas','gasPrice')})
        raw = self.account.sign_transaction(tx)
        # Any broadcast error (incl. timeout) is uncertain: may already be mined.
        try:
            sent = self.w3.eth.send_raw_transaction(raw.raw_transaction)
        except Exception as e:
            raise UncertainExecution('EVM broadcast uncertain; reconcile wallet/nonce') from e
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(sent, timeout=90, poll_latency=1)
        except Exception as e:
            raise UncertainExecution('EVM confirmation uncertain; reconcile tx '+sent.hex()) from e
        if int(receipt.status) != 1:
            raise TradingError('EVM transaction reverted: '+sent.hex())
        gas_used = int(receipt.gasUsed)*int(receipt.effectiveGasPrice)
        return sent.hex(), gas_used

    def _approve(self, token: str, spender: str, amount: int):
        c = self._contract(token)
        old = c.functions.allowance(self.wallet,spender).call()
        if old >= amount:
            return
        if old != 0:
            call = c.functions.approve(spender, 0)
            transaction = call.build_transaction({'from':self.wallet,'gasPrice':self.w3.eth.gas_price,
                 'nonce':self.w3.eth.get_transaction_count(self.wallet,'pending'),'chainId':4663})
            self._send(transaction['to'], transaction['data'], 0,transaction.get('gas'))
        call = c.functions.approve(spender, amount)
        transaction = call.build_transaction({'from':self.wallet,'gasPrice':self.w3.eth.gas_price,
             'nonce':self.w3.eth.get_transaction_count(self.wallet,'pending'),'chainId':4663})
        self._send(transaction['to'],transaction['data'],0,transaction.get('gas'))

    async def trade(self, pool: Pool, side: Side, amount: int, min_output_units: int | None = None) -> Fill:
        async with self.lock:
            src, dst = self._asset(pool,side)
            before = await asyncio.to_thread(self._balance,dst)
            firm = await self._zero_x(pool,side,amount,True)
            if not firm.get('liquidityAvailable',False):
                raise TradingError('0x firm quote unavailable')
            if side == 'sell' and min_output_units is not None:
                minimum = firm.get('minBuyAmount')
                if minimum is None or int(minimum) < min_output_units:
                    raise RiskRejection('0x firm quote minimum sell proceeds below profit floor')
            issues = firm.get('issues') or {}
            if issues.get('simulationIncomplete') or issues.get('balance') or issues.get('invalidSourcesPassed'):
                raise RiskRejection('0x preflight reported simulation/balance/source issues')
            allowance_issue = issues.get('allowance') or {}
            spender = (allowance_issue.get('spender') or firm.get('allowanceTarget') or '').lower()
            if spender and spender not in self.allowed_spenders:
                raise RiskRejection('Unknown 0x allowance spender. Never approve Settler.')
            if allowance_issue:
                if not spender:
                    raise RiskRejection('0x did not provide an allowance spender')
                await asyncio.to_thread(self._approve, src, spender, amount)
                firm = await self._zero_x(pool,side,amount,True)
                issues = firm.get('issues') or {}
                if side == 'sell' and min_output_units is not None:
                    minimum = firm.get('minBuyAmount')
                    if minimum is None or int(minimum) < min_output_units:
                        raise RiskRejection('0x firm quote became unprofitable after approval')
            if issues.get('allowance') or issues.get('balance') or issues.get('simulationIncomplete'):
                raise RiskRejection('0x firm quote not clear after approvals')
            # Token echo: when the venue names the pair it is quoting, it must
            # be the intended one. Absent fields keep the previous behaviour;
            # a present-but-different token is never signed.
            echo_sell, echo_buy = firm.get('sellToken'), firm.get('buyToken')
            if echo_sell is not None and str(echo_sell).lower() != src.lower():
                raise RiskRejection('0x firm quote sellToken does not match intent')
            if echo_buy is not None and str(echo_buy).lower() != dst.lower():
                raise RiskRejection('0x firm quote buyToken does not match intent')
            trans = firm.get('transaction') or {}
            target = str(trans.get('to') or '').lower()
            if target not in self.allowed_targets:
                raise RiskRejection('0x settlement target not in allowlist')
            if not str(trans.get('data','')).startswith('0x'):
                raise TradingError('Missing encoded swap data')
            if int(trans.get('value') or 0) != 0:
                raise RiskRejection('Unexpected ETH value in ERC20-to-ERC20 swap')
            if firm.get('sellAmount') is not None and int(firm['sellAmount']) != amount:
                raise RiskRejection('0x firm quote sell amount does not match intent')
            # A 0x quote is not proof that a token can be sold later.
            txid,gas = await asyncio.to_thread(self._send, target,trans['data'],int(trans.get('value') or 0),
                                                int(trans['gas']) if trans.get('gas') else None)
            after = await asyncio.to_thread(self._balance,dst)
            received = after-before
            if received <= 0:
                raise UncertainExecution('Transaction succeeded but output balance was not observed: '+txid)
            return Fill(txid, amount, received, gas)


class SolanaExchange(Exchange):
    chain = 'solana'
    TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'

    def __init__(self,cfg: Settings, api: Api):
        from solders.keypair import Keypair
        import base58
        self.cfg, self.api = cfg, api
        self.signer = Keypair.from_bytes(base58.b58decode(cfg.solana_private_key))
        self.wallet = str(self.signer.pubkey())
        self.lock = asyncio.Lock()

    async def _rpc(self,method,params):
        response = await self.api.post(self.cfg.solana_rpc, data={
            'jsonrpc':'2.0','id':1,'method':method,'params':params})
        if response.get('error'):
            raise TradingError(f'Solana RPC error ({method})')
        return response.get('result')

    async def validate_network(self):
        result = await self._rpc('getGenesisHash',[])
        # Full 44-char mainnet-beta genesis hash (NOT the 32-char CAIP-2 chain
        # id prefix). Settings.validate() has already checked that the expected
        # value decodes to 32 bytes. Re-verify independently before going live.
        if result != self.cfg.solana_expected_genesis:
            raise TradingError('RPC is not Solana mainnet-beta')
        mint = await self._rpc('getAccountInfo',[self.cfg.solana_usdc,
                               {'encoding':'jsonParsed','commitment':'confirmed'}])
        info = (((mint or {}).get('value') or {}).get('data') or {}).get('parsed') or {}
        if info.get('type') != 'mint' or int((info.get('info') or {}).get('decimals',-1)) != 6:
            raise TradingError('Solana USDC mint/decimals mismatch; refuse to trade')

    async def token_balance(self, pool: Pool) -> int:
        data = await self._rpc('getTokenAccountsByOwner', [self.wallet, {'mint':pool.mint},
                                                   {'encoding':'jsonParsed','commitment':'confirmed'}])
        result = 0
        for row in data.get('value',[]):
            info = row['account']['data']['parsed']['info']
            result += int(info['tokenAmount']['amount'])
        return result

    async def token_safety(self,pool: Pool) -> None:
        d = await self._rpc('getAccountInfo',[pool.mint,{'encoding':'jsonParsed','commitment':'confirmed'}])
        val = (d or {}).get('value') or {}
        if val.get('owner') != self.TOKEN_PROGRAM:
            raise RiskRejection('Unsupported Solana token program or Token-2022 extensions')
        parsed = (val.get('data') or {}).get('parsed') or {}
        if parsed.get('type') != 'mint':
            raise RiskRejection('Mint is not a standard token')
        info = parsed.get('info') or {}
        if info.get('mintAuthority') or info.get('freezeAuthority'):
            raise RiskRejection('Mint or freeze authority still active')
        if info.get('isInitialized') is not True:
            raise RiskRejection('Uninitialized mint')

    def _assets(self,pool: Pool,side: Side):
        return (self.cfg.solana_usdc,pool.mint) if side == 'buy' else (pool.mint,self.cfg.solana_usdc)

    async def _order(self,pool: Pool,side: Side,amount: int,execute: bool):
        # Jupiter Swap V2 (/order + /execute) is the current API; V1 Metis is
        # no longer actively maintained. slippageBps is always passed so the
        # response's otherAmountThreshold is derived from OUR slippage, not an
        # auto-chosen one. NEEDS LIVE VALIDATION with a funded wallet.
        src,dst=self._assets(pool,side)
        params={'inputMint':src,'outputMint':dst,'amount':str(amount),
                'slippageBps':self.cfg.slippage_bps}
        if execute:
            params['taker']=self.wallet
            params['maxSupportedTransactionVersion']=0
        return await self.api.get('https://api.jup.ag/swap/v2/order',
             params=params,headers={'x-api-key':self.cfg.jupiter_key}, retries=1)

    def _check_order_echo(self,order: dict,pool: Pool,side: Side,amount: int):
        # Reject before signing when the venue explicitly reports a different
        # pair or input amount; absent fields keep the fail-closed floor and
        # balance checks as the only guards.
        src,dst = self._assets(pool,side)
        if order.get('inputMint') is not None and str(order['inputMint']) != src:
            raise RiskRejection('Jupiter order input mint does not match intent')
        if order.get('outputMint') is not None and str(order['outputMint']) != dst:
            raise RiskRejection('Jupiter order output mint does not match intent')
        if order.get('inAmount') is not None and str(order['inAmount']) != str(amount):
            raise RiskRejection('Jupiter order input amount does not match intent')

    async def quote(self,pool: Pool,side: Side,amount: int) -> Quote:
        r = await self._order(pool,side,amount,False)
        if r.get('errorCode') or int(r.get('outAmount',0)) <= 0:
            raise TradingError('Jupiter no route or no output')
        impact = r.get('priceImpactPct')
        bps = None
        if impact is not None:
            try:
                bps = int(Decimal(str(impact))*10000)
            except Exception:
                pass
        threshold = r.get('otherAmountThreshold')
        return Quote(int(r['outAmount']), bps,
                     int(threshold) if threshold is not None else None)

    @staticmethod
    def _sign_transaction(tx_b64, signer):
        from solders.transaction import VersionedTransaction
        from solders.message import to_bytes_versioned
        t = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        header = t.message.header
        keys = list(t.message.account_keys[:header.num_required_signatures])
        if signer.pubkey() not in keys:
            raise RiskRejection('Jupiter transaction does not request the configured wallet signature')
        idx = keys.index(signer.pubkey())
        sigs = list(t.signatures)
        sigs[idx] = signer.sign_message(to_bytes_versioned(t.message))
        signed = VersionedTransaction.populate(t.message,sigs)
        return base64.b64encode(bytes(signed)).decode('ascii')

    async def _sell_with_minimum(self,pool: Pool,amount: int,min_output_units: int) -> Fill:
        # Jupiter Swap V2 /order returns otherAmountThreshold: the minimum
        # output the swap instruction enforces on-chain at the slippage we
        # requested. The sell is only signed when that enforced minimum clears
        # the profit floor, and proceeds are measured from the real stablecoin
        # balance delta, never from the quote. NEEDS LIVE VALIDATION: a funded
        # acceptance test must confirm the threshold is enforced on-chain.
        order = await self._order(pool,'sell',amount,True)
        if order.get('errorCode') or not order.get('transaction') or not order.get('requestId'):
            raise TradingError('Jupiter executable sell order unavailable')
        self._check_order_echo(order,pool,'sell',amount)
        if int(order.get('otherAmountThreshold') or 0) < min_output_units:
            raise RiskRejection('Jupiter minimum output below profit floor; keep holding')
        before = await self._stable_balance()
        signed = self._sign_transaction(order['transaction'],self.signer)
        # /execute broadcasts and confirms server-side. Any transport failure
        # after this point is uncertain: the swap may have landed anyway.
        try:
            result = await self.api.post('https://api.jup.ag/swap/v2/execute',
                headers={'x-api-key':self.cfg.jupiter_key},
                data={'signedTransaction':signed,'requestId':order['requestId']})
        except Exception as exc:
            raise UncertainExecution('Jupiter sell execution status unknown; reconcile signature/balances') from exc
        if result.get('status') != 'Success' or result.get('code') != 0:
            raise UncertainExecution('Jupiter returned a failed sell execution; reconcile onchain signature')
        after = await self._stable_balance()
        received = after-before
        if received < min_output_units:
            raise UncertainExecution('Swap confirmed but output below floor; inspect tx '+str(result.get('signature')))
        return Fill(str(result.get('signature')),amount,received)

    async def _stable_balance(self) -> int:
        data = await self._rpc('getTokenAccountsByOwner',[self.wallet,
          {'mint':self.cfg.solana_usdc},{'encoding':'jsonParsed','commitment':'confirmed'}])
        return sum(int(x['account']['data']['parsed']['info']['tokenAmount']['amount'])
                   for x in data.get('value',[]))

    async def trade(self,pool: Pool,side: Side,amount: int, min_output_units: int | None = None) -> Fill:
        async with self.lock:
            if int((await self._rpc('getBalance',[self.wallet,{'commitment':'confirmed'}]))['value']) < 15_000_000:
                raise RiskRejection('Solana wallet requires at least 0.015 SOL for fees/rent')
            if side == 'sell' and min_output_units is not None:
                return await self._sell_with_minimum(pool,amount,min_output_units)
            # Verify current destination balance; get the actual output after /execute.
            order = await self._order(pool,side,amount,True)
            if order.get('errorCode') or not order.get('transaction') or not order.get('requestId'):
                raise TradingError('Jupiter executable order unavailable')
            self._check_order_echo(order,pool,side,amount)
            signed = self._sign_transaction(order['transaction'],self.signer)
            try:
                result = await self.api.post('https://api.jup.ag/swap/v2/execute',
                    headers={'x-api-key':self.cfg.jupiter_key},
                    data={'signedTransaction':signed,'requestId':order['requestId']})
            except Exception as exc:
                raise UncertainExecution('Jupiter execution status unknown; inspect signature/balances') from exc
            if result.get('status') != 'Success' or result.get('code') != 0:
                # A reported failure is still worth reconciling; no blind retry.
                raise UncertainExecution('Jupiter returned a failed execution; reconcile onchain signature')
            received = int(result.get('totalOutputAmount') or 0)
            spent = int(result.get('totalInputAmount') or 0)
            if spent <= 0 or received <= 0:
                raise UncertainExecution('Jupiter confirmed but did not report positive wallet deltas')
            return Fill(str(result['signature']),spent,received)
