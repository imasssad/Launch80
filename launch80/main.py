"""Command entry point. Never run two instances against the same funded wallet."""
from __future__ import annotations
import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
from .config import Settings
from .discovery import GeckoPools
from .engine import Engine
from .exchange import PaperExchange, RobinhoodExchange, SolanaExchange
from .http import Api
from .garp import Garp
from .server import create_app
from .storage import Store
from .preflight import check


def parser():
    p=argparse.ArgumentParser(prog='launch80')
    sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('run'); sub.add_parser('check'); sub.add_parser('stats'); sub.add_parser('pause'); sub.add_parser('resume')
    sub.add_parser('positions'); sub.add_parser('events')
    a=sub.add_parser('abandon',help='Write off an OPEN/MANUAL_EXIT position as a total realized loss; frees capacity, never sells')
    a.add_argument('key')
    a.add_argument('--confirm-total-loss',action='store_true',required=True,
                   help='Operator accepts the full principal as realized loss')
    a.add_argument('--verify-onchain',action='store_true',required=True,
                   help='Operator independently verified the tokens are untradeable or externally disposed')
    r=sub.add_parser('reconcile',help='Manual, after independent on-chain verification')
    r.add_argument('key'); r.add_argument('action',choices=['buy-filled','buy-not-filled','sell-filled','sell-not-filled'])
    r.add_argument('--txid',default='')
    r.add_argument('--spent-atomic',type=int,default=0)
    r.add_argument('--received-atomic',type=int,default=0)
    r.add_argument('--buy-confirmed-at',type=float,default=0.0,help='Verified Unix block timestamp of buy')
    r.add_argument('--verify-onchain',action='store_true',required=True)
    return p


async def run(cfg: Settings,store: Store):
    api=Api()
    try:
        ex={}
        if cfg.enable_robinhood:
            ex['robinhood']=RobinhoodExchange(cfg,api) if cfg.live else PaperExchange('robinhood',api)
        if cfg.enable_solana:
            ex['solana']=SolanaExchange(cfg,api) if cfg.live else PaperExchange('solana',api)
        engine=Engine(cfg,store,GeckoPools(api,cfg.page_count),ex,
                      garp=Garp(api,cfg.garp_require_launchpad) if cfg.garp_enrich else None)
        # Monitoring API runs independently so it can display a blocked/recovery state.
        import uvicorn
        web_cfg=uvicorn.Config(create_app(store,cfg),host=os.getenv('HTTP_HOST','127.0.0.1'),
                              port=int(os.getenv('HTTP_PORT','8088')),log_level='warning')
        server=uvicorn.Server(web_cfg)
        task=asyncio.create_task(server.serve())
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            try: loop.add_signal_handler(sig,engine.stop)
            except NotImplementedError: pass
        try: await engine.run()
        finally:
            server.should_exit=True
            await task
    finally:
        await api.close()


def main():
    args=parser().parse_args()
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        cfg=Settings.load()
        store=Store(cfg.db_path)
    except Exception as e:
        raise SystemExit(f'Invalid configuration: {e}')
    try:
        if args.cmd=='run':
            # Single-writer process lock (Linux/macOS). Container volume persists it.
            lockfile=cfg.db_path+'.process.lock'
            with open(lockfile,'w') as fh:
                try: fcntl.flock(fh.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: raise SystemExit('Another launch80 process is already running')
                asyncio.run(run(cfg,store))
        elif args.cmd=='check':
            report=asyncio.run(check(cfg))
            print(json.dumps(report,indent=2))
            if not report['ok']: raise SystemExit(2)
        elif args.cmd=='stats': print(json.dumps(store.stats(),indent=2))
        elif args.cmd=='positions': print(json.dumps(store.all_positions(),indent=2))
        elif args.cmd=='events': print(json.dumps(store.events(),indent=2))
        elif args.cmd=='pause':
            store.set_flag('paused','1');store.log('OPERATOR_PAUSE');print('New buys paused; exits continue.')
        elif args.cmd=='resume':
            if store.has_unknowns(): raise SystemExit('Unreconciled trade exists; resolve it first.')
            store.set_flag('paused','0');store.log('OPERATOR_RESUME');print('New buys resumed.')
        elif args.cmd=='abandon':
            # Human decision only. Keeps the profit-only invariant: the bot
            # never sells at a loss; the operator declares the loss instead.
            if not store.get(args.key): raise SystemExit('Unknown position key')
            if not store.abandon(args.key,'Operator wrote off position after independent verification'):
                raise SystemExit('Only OPEN or MANUAL_EXIT positions can be abandoned')
            store.log('OPERATOR_ABANDON',args.key,'Principal counted as realized loss; capacity freed')
            print('Position abandoned. Principal now counts toward the daily realized loss cap.')
        elif args.cmd=='reconcile':
            # Human operator must independently verify actual transactions & balances.
            if not args.verify_onchain:
                raise SystemExit('Verification flag required')
            row=store.get(args.key)
            if not row: raise SystemExit('Unknown position key')
            if cfg.live and args.action in ('buy-filled','sell-filled'):
                # --verify-onchain alone is a human attestation; in live mode
                # the txid is additionally confirmed against the chain and the
                # reconciliation is refused when it cannot be (fail closed).
                from .verify import confirmed_onchain
                if not asyncio.run(confirmed_onchain(cfg,row['chain'],args.txid)):
                    raise SystemExit('Transaction not independently confirmed on-chain '
                                     '(unknown, failed, or RPC unreachable); refusing reconciliation')
            ok=False
            if args.action=='buy-filled':
                if row['status']=='RECOVERY_BUY' and args.txid and args.spent_atomic>0 and args.received_atomic>0 and 0<args.buy_confirmed_at<=__import__('time').time():
                    store.open_position(args.key,args.spent_atomic,args.received_atomic,args.txid,cfg.hold_seconds,buy_at=args.buy_confirmed_at)
                    ok=True
            elif args.action=='buy-not-filled':
                ok=store.reconcile_failed_buy(args.key,'Operator independently verified no buy executed')
            elif args.action=='sell-filled':
                if args.txid and args.received_atomic>=0:
                    ok=store.reconcile_confirmed_sell(args.key,args.txid,args.received_atomic)
            elif args.action=='sell-not-filled':
                ok=store.reconcile_no_sell(args.key,'Operator independently verified no sell executed')
            if not ok: raise SystemExit('Incompatible status or missing verified transaction details')
            store.log('OPERATOR_RECONCILIATION',args.key,args.action)
            print('Reconciled. Review stats before resuming buys.')
    finally: store.close()

if __name__=='__main__': main()
