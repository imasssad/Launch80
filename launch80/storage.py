"""SQLite journal. BUYING/SELLING are durable pending intents: never replay blindly."""
from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from .models import Pool

DDL = '''
CREATE TABLE IF NOT EXISTS positions (
  key TEXT PRIMARY KEY,
  chain TEXT NOT NULL,
  pool TEXT NOT NULL,
  mint TEXT NOT NULL,
  symbol TEXT,
  dex TEXT,
  liquidity_usd TEXT,
  created_at REAL NOT NULL,
  status TEXT NOT NULL,
  attempted_at REAL NOT NULL,
  buy_at REAL,
  sell_due_at REAL,
  sell_at REAL,
  buy_tx TEXT,
  sell_tx TEXT,
  buy_spent_units INTEGER NOT NULL DEFAULT 0,
  buy_gas_units TEXT NOT NULL DEFAULT '0',
  sell_gas_units TEXT NOT NULL DEFAULT '0',
  bought_units TEXT NOT NULL DEFAULT '0',
  sell_received_units INTEGER NOT NULL DEFAULT 0,
  before_token_balance TEXT NOT NULL DEFAULT '0',
  last_error TEXT,
  sell_attempts INTEGER NOT NULL DEFAULT 0,
  next_sell_retry_at REAL NOT NULL DEFAULT 0,
  last_exit_quote_units TEXT,
  last_exit_quote_at REAL,
  underwater_since REAL,
  underwater_checks INTEGER NOT NULL DEFAULT 0,
  underwater_alerted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS positions_status_due ON positions(status, sell_due_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
  kind TEXT NOT NULL, key TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
'''

class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA busy_timeout=10000')
            self.db.executescript(DDL)
            cols={x['name'] for x in self.db.execute('PRAGMA table_info(positions)')}
            migrations = {
                'buy_gas_units': "TEXT NOT NULL DEFAULT '0'",
                'sell_gas_units': "TEXT NOT NULL DEFAULT '0'",
                'last_exit_quote_units': 'TEXT',
                'last_exit_quote_at': 'REAL',
                'underwater_since': 'REAL',
                'underwater_checks': 'INTEGER NOT NULL DEFAULT 0',
                'underwater_alerted': 'INTEGER NOT NULL DEFAULT 0',
            }
            for name, spec in migrations.items():
                if name not in cols:
                    self.db.execute(f'ALTER TABLE positions ADD COLUMN {name} {spec}')
            self.db.execute("INSERT OR IGNORE INTO flags(name,value) VALUES('paused','0')")

    def close(self):
        self.db.close()

    def log(self, kind, key='', detail=''):
        with self.lock:
            self.db.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                            (time.time(), kind, key, str(detail)[:500]))

    def events(self, count=100):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (min(count, 300),))]

    def all_positions(self, count=100):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM positions ORDER BY attempted_at DESC LIMIT ?', (min(count, 300),))]

    def get(self, key):
        with self.lock:
            row = self.db.execute('SELECT * FROM positions WHERE key=?', (key,)).fetchone()
            return dict(row) if row else None

    def flag(self, name):
        with self.lock:
            row = self.db.execute('SELECT value FROM flags WHERE name=?', (name,)).fetchone()
            return row['value'] if row else None

    def set_flag(self, name, value):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO flags(name,value) VALUES(?,?)', (name, str(value)))

    def verify_mode(self, mode: str):
        """Refuse to trade on a DB containing positions from another mode.

        Important when deploying live from a paper-trading installation.
        Existing pre-v1.1 databases with positions cannot be proven to be
        paper/live; use a fresh live database and archive the old one.
        """
        if mode not in ('live','paper'):
            raise ValueError('Unknown trade mode')
        with self.lock:
            recorded = self.flag('trading_mode')
            if recorded is not None and recorded != mode:
                raise RuntimeError(f'Database is {recorded}; refusing {mode} execution; use a separate DB')
            if recorded is None:
                existing = self.db.execute('SELECT COUNT(*) n FROM positions').fetchone()['n']
                if existing:
                    raise RuntimeError('Legacy database contains untagged positions; never switch modes on this DB')
                self.set_flag('trading_mode',mode)

    def claim(self, pool: Pool, before_balance: int, intended_spend_units: int = 0) -> bool:
        # Record the intended spend immediately so BUYING/RECOVERY_BUY rows
        # count toward exposure_usd() while the fill is in flight or unknown;
        # open_position() replaces it with the actual filled amount.
        with self.lock:
            cur = self.db.execute('''INSERT OR IGNORE INTO positions(
                key,chain,pool,mint,symbol,dex,liquidity_usd,created_at,
                status,attempted_at,before_token_balance,buy_spent_units)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (pool.key,pool.chain,pool.pool,pool.mint,pool.symbol,pool.dex,
                 str(pool.liquidity_usd), pool.created_at,'BUYING',time.time(),
                 str(before_balance),int(intended_spend_units)))
            return cur.rowcount == 1

    def open_position(self, key, spent: int, received: int, txid: str, hold_seconds: int, buy_at=None, gas_units=0):
        t = buy_at or time.time()
        with self.lock:
            self.db.execute('''UPDATE positions SET status='OPEN', buy_at=?,sell_due_at=?,
                buy_tx=?,buy_spent_units=?,bought_units=?,buy_gas_units=?,last_error=NULL WHERE key=?
                AND status IN ('BUYING','RECOVERY_BUY')''',
                (t,t+hold_seconds,txid,spent,str(received),str(gas_units),key))

    def defer_unprofitable(self, key: str, quote_units: int, delay: int,
                           required_units: int, now: float | None = None) -> dict | None:
        # Preserve OPEN and capacity/exposure accounting even for indefinite holds.
        t = time.time() if now is None else now
        with self.lock:
            self.db.execute("""UPDATE positions SET next_sell_retry_at=?,
                last_exit_quote_units=?,last_exit_quote_at=?,
                underwater_since=COALESCE(underwater_since,?),
                underwater_checks=underwater_checks+1,
                last_error=? WHERE key=? AND status='OPEN'""",
                (t+delay,str(quote_units),t,t,
                 f'Exit held: conservative proceeds {quote_units} below floor {required_units}',key))
            return self.get(key)

    def mark_underwater_alerted(self,key: str):
        with self.lock:
            self.db.execute("UPDATE positions SET underwater_alerted=1 WHERE key=? AND status='OPEN'",(key,))

    def record_acceptable_exit_quote(self,key: str,quote_units: int,now: float | None = None):
        t = time.time() if now is None else now
        with self.lock:
            self.db.execute("""UPDATE positions SET last_exit_quote_units=?,last_exit_quote_at=?,
              underwater_since=NULL,underwater_alerted=0,last_error=NULL WHERE key=? AND status='OPEN'""",
              (str(quote_units),t,key))

    def defer_quote_unavailable(self,key: str,reason: str,delay: int):
        # A failed read-only quote is not a failed sell attempt.
        with self.lock:
            self.db.execute("""UPDATE positions SET next_sell_retry_at=?,last_error=?
                 WHERE key=? AND status='OPEN'""",
                 (time.time()+delay,str(reason)[:400],key))

    def reset_sell_after_price_change(self,key: str,reason: str,delay: int):
        # Never re-broadcast an uncertain tx. Only use for an explicitly rejected
        # quote before any settlement tx is sent.
        with self.lock:
            self.db.execute("""UPDATE positions SET status='OPEN',next_sell_retry_at=?,
                 sell_attempts=MAX(0,sell_attempts-1),last_error=? WHERE key=? AND status='SELLING'""",
                 (time.time()+delay,str(reason)[:400],key))

    def mark_selling(self, key):
        with self.lock:
            cur = self.db.execute('''UPDATE positions SET status='SELLING',sell_attempts=sell_attempts+1
                WHERE key=? AND status='OPEN' ''',(key,))
            return cur.rowcount == 1

    def close_position(self, key, received: int, txid: str, gas_units=0):
        with self.lock:
            self.db.execute('''UPDATE positions SET status='CLOSED',sell_at=?,sell_tx=?,
                sell_received_units=?,sell_gas_units=?,last_error=NULL WHERE key=? AND status='SELLING' ''',
                (time.time(),txid,received,str(gas_units),key))

    def set_error(self, key, status, reason):
        with self.lock:
            self.db.execute('UPDATE positions SET status=?,last_error=? WHERE key=?',
                            (status,str(reason)[:400],key))

    def retry_sell(self, key, reason, delay):
        # SELLING already increments once in mark_selling; avoid double counting.
        with self.lock:
            self.db.execute("""UPDATE positions SET status='OPEN',last_error=?,
                next_sell_retry_at=? WHERE key=? AND status='SELLING'""",
                (str(reason)[:400],time.time()+delay,key))

    def due(self, now: float, limit=10):
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT * FROM positions WHERE status='OPEN'
                  AND sell_due_at <= ? AND next_sell_retry_at <= ? ORDER BY sell_due_at LIMIT ?''',
                  (now,now,limit))]

    def has_unknowns(self):
        with self.lock:
            row = self.db.execute("SELECT COUNT(*) AS n FROM positions WHERE status LIKE 'RECOVERY_%' OR status IN ('BUYING','SELLING','MANUAL_EXIT')").fetchone()
            return row['n'] > 0

    def pending_on_restart(self):
        with self.lock:
            self.db.execute("UPDATE positions SET status='RECOVERY_BUY',last_error='Restart during buy; reconcile before trading' WHERE status='BUYING'")
            self.db.execute("UPDATE positions SET status='RECOVERY_SELL',last_error='Restart during sell; reconcile before trading' WHERE status='SELLING'")

    def exposure_usd(self):
        with self.lock:
            rows = self.db.execute("SELECT buy_spent_units FROM positions WHERE status IN ('OPEN','BUYING','SELLING','RECOVERY_BUY','RECOVERY_SELL','MANUAL_EXIT')").fetchall()
            return sum(r['buy_spent_units'] for r in rows)/1_000_000

    def active_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) AS n FROM positions WHERE status IN ('OPEN','BUYING','SELLING','RECOVERY_BUY','RECOVERY_SELL','MANUAL_EXIT')").fetchone()['n']

    def hourly_attempts(self):
        with self.lock:
            return self.db.execute('SELECT COUNT(*) AS n FROM positions WHERE attempted_at >= ?', (time.time()-3600,)).fetchone()['n']

    def daily_realized_usd(self):
        start = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.lock:
            rows = self.db.execute("SELECT buy_spent_units,sell_received_units FROM positions WHERE status='CLOSED' AND sell_at>=?",(start,)).fetchall()
            return sum((r['sell_received_units']-r['buy_spent_units']) for r in rows)/1_000_000

    def stats(self):
        with self.lock:
            status = {r['status']:r['n'] for r in self.db.execute('SELECT status,COUNT(*) n FROM positions GROUP BY status')}
            return dict(status=status, exposure_usd=self.exposure_usd(),
                        utc_daily_realized_usd=self.daily_realized_usd(),
                        paused=self.flag('paused') == '1', unknown_positions=self.has_unknowns())

    def reconcile_failed_buy(self, key: str, reason: str):
        with self.lock:
            cur = self.db.execute("UPDATE positions SET status='FAILED_BUY',last_error=? WHERE key=? AND status='RECOVERY_BUY'",(reason[:400],key))
            return cur.rowcount == 1

    def reconcile_no_sell(self, key: str, reason: str):
        with self.lock:
            cur = self.db.execute("UPDATE positions SET status='OPEN',next_sell_retry_at=0,sell_attempts=0,last_error=? WHERE key=? AND status IN ('RECOVERY_SELL','MANUAL_EXIT')",(reason[:400],key))
            return cur.rowcount == 1

    def reconcile_confirmed_sell(self,key: str,txid: str,received_units: int):
        with self.lock:
            cur = self.db.execute("UPDATE positions SET status='CLOSED',sell_at=?,sell_tx=?,sell_received_units=?,last_error=NULL WHERE key=? AND status IN ('RECOVERY_SELL','MANUAL_EXIT')",(time.time(),txid,received_units,key))
            return cur.rowcount == 1
