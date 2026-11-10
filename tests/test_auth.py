"""Auth: sessions, CSRF, throttling, TOTP, and the read-route gate."""
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from launch80 import auth as authlib
from launch80.server import create_app
from launch80.storage import Store

PW = 'correct-horse-battery-staple'
SECRET = 's' * 32


@pytest.fixture
def secured(cfg):
    c = replace(cfg, admin_password=PW, session_secret=SECRET, cookie_secure=False)
    store = Store(c.db_path)
    yield TestClient(create_app(store, c)), store, c
    store.close()


@pytest.fixture
def openmode(cfg):
    store = Store(cfg.db_path)
    yield TestClient(create_app(store, cfg)), store
    store.close()


def signin(client, password=PW, otp=None):
    return client.post('/api/auth/login', json={'password': password, 'otp': otp})


# --- the gate ---------------------------------------------------------------

@pytest.mark.parametrize('route', ['/stats', '/positions', '/events'])
def test_read_routes_require_session(secured, route):
    client, _, _ = secured
    assert client.get(route).status_code == 401


def test_health_stays_public_and_leaks_no_positions(secured):
    client, _, _ = secured
    r = client.get('/health')
    assert r.status_code == 200
    body = r.json()
    # Pinned deliberately: /health is the one public route, so any new field
    # here becomes public. Adding one must be a conscious edit to this list.
    assert set(body) == {'ok', 'mode', 'exit_policy', 'earliest_exit_seconds',
                         'paused', 'unreconciled', 'engine_alive',
                         'last_poll_age_seconds'}


def test_login_then_read(secured):
    client, _, _ = secured
    assert signin(client).status_code == 200
    assert client.get('/positions').status_code == 200


def test_wrong_password_rejected(secured):
    client, _, _ = secured
    assert signin(client, 'wrong-but-long-enough').status_code == 401
    assert client.get('/stats').status_code == 401


def test_routes_open_when_auth_disabled(openmode):
    client, _ = openmode
    assert client.get('/stats').status_code == 200


# --- session lifecycle ------------------------------------------------------

def test_token_is_never_stored_in_the_journal(secured):
    client, store, c = secured
    token = signin(client).cookies.get('launch80_session') or \
        client.cookies.get('launch80_session')
    rows = store.admin_sessions()
    assert len(rows) == 1
    assert token and rows[0]['id'] not in (token, token[:12])


def test_logout_revokes(secured):
    client, _, _ = secured
    csrf = signin(client).json()['csrf']
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/stats').status_code == 401


def test_expired_session_rejected(secured):
    client, store, _ = secured
    signin(client)
    store.db.execute('UPDATE admin_sessions SET expires_at=?', (time.time() - 1,))
    assert client.get('/stats').status_code == 401


def test_idle_session_rejected(secured):
    client, store, _ = secured
    signin(client)
    store.db.execute('UPDATE admin_sessions SET last_seen_at=?',
                     (time.time() - authlib.SESSION_IDLE - 10,))
    assert client.get('/stats').status_code == 401


def test_login_rotates_session(secured):
    client, store, _ = secured
    signin(client)
    first = store.admin_sessions()[0]['id']
    signin(client)
    live = [s['id'] for s in store.admin_sessions()]
    assert first not in live and len(live) == 1


# --- CSRF -------------------------------------------------------------------

def test_mutating_route_needs_csrf(secured):
    client, _, _ = secured
    signin(client)
    assert client.post('/pause').status_code in (401, 403)


def test_mutating_route_with_csrf_works(secured):
    client, store, _ = secured
    csrf = signin(client).json()['csrf']
    assert client.post('/pause', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert store.flag('paused') == '1'


def test_wrong_csrf_rejected(secured):
    client, _, _ = secured
    signin(client)
    assert client.post('/pause', headers={'X-CSRF-Token': 'nope'}).status_code == 403


# --- throttling -------------------------------------------------------------

def test_repeated_failures_lock_the_ip_out():
    lim = authlib.LoginLimiter(free_failures=3, base=10.0)
    for _ in range(3):
        lim.failure('1.2.3.4')
    assert lim.retry_after('1.2.3.4') > 0
    assert lim.retry_after('5.6.7.8') == 0, 'another IP must be unaffected'


def test_success_clears_the_lockout():
    lim = authlib.LoginLimiter(free_failures=1, base=10.0)
    lim.failure('1.2.3.4')
    lim.success('1.2.3.4')
    assert lim.retry_after('1.2.3.4') == 0


def test_global_throttle_never_locks_out_the_owner():
    lim = authlib.LoginLimiter(global_limit=5)
    lim.allowlist = ('9.9.9.9',)
    for i in range(10):
        lim.failure(f'10.0.0.{i}')
    assert lim.retry_after('8.8.8.8') > 0, 'unknown IP should be throttled'
    assert lim.retry_after('9.9.9.9') == 0, 'allowlisted owner must never be locked out'


def test_limiter_memory_is_bounded():
    lim = authlib.LoginLimiter(max_entries=50)
    for i in range(500):
        lim.failure(f'10.1.{i // 256}.{i % 256}')
    assert len(lim.per_ip) <= 50


# --- TOTP -------------------------------------------------------------------

def test_totp_disabled_when_no_secret():
    assert authlib.totp_ok('', None) is True


def test_totp_rejects_malformed():
    assert authlib.totp_ok('BASE32SECRET3232', 'abc') is False
    assert authlib.totp_ok('BASE32SECRET3232', None) is False


def test_totp_verifies_and_rejects_replay():
    pyotp = pytest.importorskip('pyotp')
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    authlib._TOTP_LAST_COUNTER['value'] = -1
    assert authlib.totp_ok(secret, code, consume=True) is True
    assert authlib.totp_ok(secret, code, consume=True) is False, 'replay must fail'


# --- config -----------------------------------------------------------------

def test_weak_admin_password_rejected(cfg):
    with pytest.raises(ValueError, match='ADMIN_PASSWORD'):
        replace(cfg, admin_password='short', session_secret=SECRET).validate()


def test_admin_password_requires_session_secret(cfg):
    with pytest.raises(ValueError, match='SESSION_SECRET'):
        replace(cfg, admin_password=PW, session_secret='').validate()


def test_totp_requires_admin_password(cfg):
    with pytest.raises(ValueError, match='TOTP_SECRET'):
        replace(cfg, totp_secret='BASE32SECRET3232').validate()


# --- headers ----------------------------------------------------------------

def test_responses_are_uncacheable_and_unframeable(secured):
    client, _, _ = secured
    h = client.get('/health').headers
    assert 'no-store' in h['cache-control']
    assert h['x-frame-options'] == 'DENY'


# --- dashboard shell --------------------------------------------------------

def test_dashboard_shell_is_public_but_carries_no_data(secured, pool):
    """The login form must render signed-out, and embed no stored data.

    Field names appear in the page's JavaScript by necessity; what must never
    appear is anything read from the journal. Asserted by seeding a position
    and checking the shell is byte-identical.
    """
    client, store, _ = secured
    before = client.get('/')
    assert before.status_code == 200
    assert 'text/html' in before.headers['content-type']
    assert 'Operator sign-in' in before.text
    assert 'no-store' in before.headers['cache-control']

    store.claim(pool, 0, intended_spend_units=5_000_000)
    store.open_position(pool.key, 5_000_000, 123, 'PAPER-secret-txid', 80)
    after = client.get('/')
    assert after.text == before.text, 'shell must not vary with journal contents'
    for secret in ('PAPER-secret-txid', pool.mint, pool.symbol):
        assert secret not in after.text


def test_dashboard_data_still_requires_session_after_loading_shell(secured):
    client, _, _ = secured
    assert client.get('/').status_code == 200
    assert client.get('/positions').status_code == 401


# --- engine liveness --------------------------------------------------------

def test_health_reports_dead_engine(secured):
    """uvicorn survives an engine crash, so /health must not read as healthy."""
    client, store, _ = secured
    assert client.get('/health').json()['engine_alive'] is False, 'no heartbeat yet'

    import time
    store.set_flag('engine_heartbeat', str(time.time()))
    body = client.get('/health').json()
    assert body['engine_alive'] is True and body['ok'] is True

    store.set_flag('engine_heartbeat', str(time.time() - 100000))
    body = client.get('/health').json()
    assert body['engine_alive'] is False and body['ok'] is False


# --- proxy-aware client IP --------------------------------------------------

def _req(peer, headers=None):
    scope_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {'type': 'http', 'headers': scope_headers, 'client': (peer, 1234)}
    from starlette.requests import Request as SRequest
    return SRequest(scope)


def test_forwarded_ip_trusted_only_from_loopback():
    from launch80.auth import client_ip
    # Our own reverse proxy on this host: trust the forwarded client.
    assert client_ip(_req('127.0.0.1', {'X-Forwarded-For': '203.0.113.9'})) == '203.0.113.9'
    # First hop wins when a chain is present.
    assert client_ip(_req('127.0.0.1', {'X-Forwarded-For': '203.0.113.9, 10.0.0.1'})) == '203.0.113.9'


def test_forwarded_ip_ignored_from_remote_peer():
    """A remote client must not spoof its IP to dodge the login throttle."""
    from launch80.auth import client_ip
    assert client_ip(_req('198.51.100.7', {'X-Forwarded-For': '127.0.0.1'})) == '198.51.100.7'


def test_client_ip_without_forwarded_header():
    from launch80.auth import client_ip
    assert client_ip(_req('198.51.100.7')) == '198.51.100.7'
