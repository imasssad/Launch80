"""Operator authentication: server-side sessions, CSRF, login throttling, TOTP, audit.

Modelled on the auth system in the operator's volt.everset project, adapted to
this bot's SQLite journal instead of SQLAlchemy.

Design notes that matter:
* The session TOKEN is never stored. Only its SHA-256 hash is, so a dump of the
  journal yields nothing usable.
* Throttling cannot lock the owner out of their own trading system: per-IP
  backoff only penalises the failing IP, and the global credential-stuffing
  throttle exempts OWNER_IP_ALLOWLIST and recently-successful IPs.
* Read routes are protected too. Positions and their sell timers are the
  sensitive data here, not just the controls.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

SESSION_ABSOLUTE = 8 * 3600   # hard lifetime
SESSION_IDLE = 2 * 3600       # idle timeout
TOUCH_EVERY = 60              # seconds between last_seen writes


def cookie_name(secure: bool) -> str:
    # __Host- requires Secure, Path=/ and no Domain; the browser then refuses
    # any subdomain- or http-set cookie using this name.
    return '__Host-launch80_session' if secure else 'launch80_session'


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


LOOPBACK = {'127.0.0.1', '::1', 'localhost', 'testclient'}


def client_ip(request: Request) -> str:
    """Real client IP, honouring X-Forwarded-For only from a local proxy.

    The monitor binds to 127.0.0.1, so the only thing that can open a
    connection is a reverse proxy on this host. A forwarded header is
    therefore trustworthy exactly when the peer is loopback; from anywhere
    else it is attacker-controlled and ignored, so a remote client cannot
    spoof an IP to dodge the login throttle.
    """
    peer = request.client.host if request.client else 'unknown'
    if peer in LOOPBACK:
        forwarded = request.headers.get('x-forwarded-for', '')
        first = forwarded.split(',')[0].strip()
        if first:
            return first[:64]
    return peer


def user_agent(request: Request) -> str:
    return request.headers.get('user-agent', '')[:160]


# ---------------------------------------------------------------- credentials
def password_ok(candidate: str, expected: str) -> bool:
    # Compare bytes: hmac.compare_digest raises TypeError on non-ASCII str.
    return bool(expected) and hmac.compare_digest(
        candidate.encode('utf-8'), expected.encode('utf-8'))


_TOTP_LAST_COUNTER = {'value': -1}


def totp_ok(secret: str, code: str | None, *, consume: bool = False) -> bool:
    """Verify a 6-digit TOTP (+/-1 step). ``consume`` rejects replay of a used code."""
    if not secret:
        return True
    if not code or not code.isdigit() or len(code) != 6:
        return False
    try:
        import pyotp
    except ImportError:
        # Fail closed: a configured second factor that cannot be checked must
        # never silently pass.
        return False
    from datetime import datetime, timezone
    totp = pyotp.TOTP(secret)
    current = totp.timecode(datetime.now(timezone.utc))
    for counter in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.generate_otp(counter), code):
            if consume:
                if counter <= _TOTP_LAST_COUNTER['value']:
                    return False
                _TOTP_LAST_COUNTER['value'] = counter
            return True
    return False


# ---------------------------------------------------------------- throttling
class LoginLimiter:
    """Per-IP exponential backoff plus a global throttle that cannot lock out the owner.

    * Each IP gets ``free_failures`` free attempts, then a lockout of ``base``
      seconds doubling per further failure (capped). An attacker can therefore
      only lock out their own IP.
    * If failures across all IPs exceed ``global_limit`` within
      ``global_window``, logins from IPs that are neither allowlisted nor
      recently successful are throttled too.
    * Memory is bounded (LRU), so the limiter is not itself a DoS vector.
    """

    def __init__(self, max_entries=10_000, free_failures=5, base=30.0, cap=3600.0,
                 global_limit=60, global_window=900.0, known_ttl=30 * 86400):
        self.max_entries, self.free, self.base, self.cap = max_entries, free_failures, base, cap
        self.global_limit, self.global_window, self.known_ttl = global_limit, global_window, known_ttl
        self.per_ip: OrderedDict[str, dict] = OrderedDict()
        self.known: OrderedDict[str, float] = OrderedDict()
        self.global_failures: list[float] = []
        self.allowlist: tuple[str, ...] = ()

    def _trim(self, table: OrderedDict):
        while len(table) > self.max_entries:
            table.popitem(last=False)

    def owner_ip(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for entry in self.allowlist:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def retry_after(self, ip: str) -> float:
        t = time.time()
        entry = self.per_ip.get(ip)
        if entry and entry['until'] > t:
            return entry['until'] - t
        self.global_failures = [x for x in self.global_failures if x > t - self.global_window]
        if len(self.global_failures) >= self.global_limit:
            trusted = self.owner_ip(ip) or (self.known.get(ip, 0) > t - self.known_ttl)
            if not trusted:
                return max(1.0, self.global_failures[0] + self.global_window - t)
        return 0.0

    def failure(self, ip: str) -> float:
        t = time.time()
        entry = self.per_ip.pop(ip, None) or {'count': 0, 'until': 0.0, 'at': t}
        if t - entry['at'] > 86400:
            entry = {'count': 0, 'until': 0.0, 'at': t}
        entry['count'] += 1
        entry['at'] = t
        if entry['count'] >= self.free:
            entry['until'] = t + min(self.cap, self.base * 2 ** (entry['count'] - self.free))
        self.per_ip[ip] = entry
        self._trim(self.per_ip)
        self.global_failures.append(t)
        if len(self.global_failures) > self.global_limit * 4:
            self.global_failures = self.global_failures[-self.global_limit * 2:]
        return max(0.0, entry['until'] - t)

    def success(self, ip: str):
        self.per_ip.pop(ip, None)
        self.known.pop(ip, None)
        self.known[ip] = time.time()
        self._trim(self.known)


# ---------------------------------------------------------------- sessions
class Auth:
    """Binds the limiter, the journal and the config into request helpers."""

    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.limiter = LoginLimiter()
        self.limiter.allowlist = cfg.owner_ip_allowlist

    @property
    def cookie(self) -> str:
        return cookie_name(self.cfg.cookie_secure)

    def enabled(self) -> bool:
        return bool(self.cfg.admin_password)

    def create_session(self, request: Request) -> str:
        token = secrets.token_urlsafe(32)
        self.store.create_admin_session(hash_token(token), SESSION_ABSOLUTE,
                                        client_ip(request)[:64], user_agent(request))
        return token

    def revoke(self, sid: str):
        self.store.revoke_admin_session(sid)

    def revoke_all(self) -> int:
        return self.store.revoke_all_admin_sessions()

    def session_id(self, request: Request) -> str | None:
        token = request.cookies.get(self.cookie, '')
        if not token or len(token) > 128:
            return None
        return self.store.touch_admin_session(hash_token(token), SESSION_IDLE, TOUCH_EVERY)

    def csrf_token(self, sid: str) -> str:
        return hmac.new(self.cfg.session_secret.encode(),
                        ('csrf.' + sid).encode(), hashlib.sha256).hexdigest()

    def authorized(self, request: Request) -> str:
        sid = self.session_id(request)
        if not sid:
            raise HTTPException(401, 'Sign in required')
        request.state.actor = sid[:12]
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            supplied = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(supplied.encode('utf-8', 'surrogateescape'),
                                       self.csrf_token(sid).encode()):
                raise HTTPException(403, 'Missing or invalid CSRF token')
        return sid

    def audit(self, request: Request, action: str, result: str):
        actor = getattr(request.state, 'actor', '') or 'anonymous'
        self.store.log('AUDIT', actor[:40],
                       f'{action} -> {result} from {client_ip(request)[:64]}')
