"""Operator monitoring and controls.

Two deployment shapes:

* ADMIN_PASSWORD unset (default): no session auth. The monitor is then
  localhost-only by contract - its read routes expose open positions and their
  sell timers, so it must never be reachable from a network in this mode.
* ADMIN_PASSWORD set: every route requires a signed-in session, so the
  dashboard can be served through a reverse proxy. Sessions are server-side
  and hashed, mutating routes require CSRF, logins are throttled, and an
  optional TOTP second factor applies.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from . import __version__, auth as authlib
from .config import Settings
from .storage import Store


def create_app(store: Store, cfg: Settings) -> FastAPI:
    app = FastAPI(title='Launch80 Monitoring', version=__version__,
                  docs_url=None, redoc_url=None)
    auth = authlib.Auth(store, cfg)

    @app.middleware('http')
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # An authenticated view of live positions must never sit in a shared
        # cache, and must not be framed.
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'")
        return response

    def guard(request: Request) -> str:
        """Session gate. A no-op only when dashboard auth is disabled."""
        if not auth.enabled():
            return 'local'
        return auth.authorized(request)

    @app.get('/')
    def index():
        # The shell is public; every data route behind it requires a session.
        # Serving it unauthenticated is what lets the browser render a login
        # form at all.
        page = Path(__file__).parent / 'web' / 'index.html'
        if not page.is_file():
            raise HTTPException(404, 'Dashboard UI not installed')
        return FileResponse(page, media_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    # ---------------------------------------------------------------- auth
    @app.get('/api/auth/session')
    def auth_session(request: Request):
        if not auth.enabled():
            return {'authenticated': True, 'auth_disabled': True, 'csrf': None}
        sid = auth.session_id(request)
        return {'authenticated': bool(sid), 'auth_disabled': False,
                'csrf': auth.csrf_token(sid) if sid else None,
                'totp_required': bool(cfg.totp_secret)}

    @app.post('/api/auth/login')
    def login(request: Request, response: Response, payload: dict = Body(...)):
        if not auth.enabled():
            raise HTTPException(404, 'Dashboard auth is not configured')
        ip = authlib.client_ip(request)
        wait = auth.limiter.retry_after(ip)
        if wait > 0:
            raise HTTPException(429, f'Too many failed sign-in attempts; retry in {int(wait) + 1}s')
        password = str(payload.get('password', ''))
        otp = payload.get('otp')
        if (not authlib.password_ok(password, cfg.admin_password)
                or not authlib.totp_ok(cfg.totp_secret, otp, consume=True)):
            locked = auth.limiter.failure(ip)
            store.log('AUTH_FAILED', '', f'ip={ip[:64]}; lockout={int(locked)}s')
            raise HTTPException(401, 'Invalid credentials')
        auth.limiter.success(ip)
        # Rotate: any session presented with the login request is revoked, so a
        # fixated cookie cannot survive a successful sign-in.
        old = request.cookies.get(auth.cookie)
        if old:
            auth.revoke(authlib.hash_token(old))
        token = auth.create_session(request)
        sid = authlib.hash_token(token)
        request.state.actor = sid[:12]
        response.set_cookie(auth.cookie, token, httponly=True, secure=cfg.cookie_secure,
                            samesite='strict', max_age=authlib.SESSION_ABSOLUTE, path='/')
        auth.audit(request, 'login', 'ok')
        return {'authenticated': True, 'csrf': auth.csrf_token(sid)}

    @app.post('/api/auth/logout')
    def logout(request: Request, response: Response, sid: str = Depends(guard)):
        if auth.enabled():
            auth.revoke(sid)
        response.delete_cookie(auth.cookie, path='/', secure=cfg.cookie_secure,
                               httponly=True, samesite='strict')
        auth.audit(request, 'logout', 'ok')
        return {'authenticated': False}

    @app.post('/api/auth/logout-everywhere')
    def logout_all(request: Request, sid: str = Depends(guard)):
        count = auth.revoke_all() if auth.enabled() else 0
        auth.audit(request, 'logout-everywhere', f'{count} revoked')
        return {'revoked': count}

    @app.get('/api/auth/sessions')
    def sessions(sid: str = Depends(guard)):
        return store.admin_sessions() if auth.enabled() else []

    # ---------------------------------------------------------------- monitor
    @app.get('/health')
    def health():
        # Deliberately unauthenticated: liveness only, no position data. This is
        # what a public status page may expose.
        import time as _time
        beat = store.flag('engine_heartbeat')
        age = _time.time() - float(beat) if beat else None
        # Stale after four poll intervals: long enough to ride out a slow feed,
        # short enough that a crashed engine does not read as healthy.
        engine_alive = age is not None and age < max(120.0, cfg.poll_seconds * 4)
        return {'ok': engine_alive, 'mode': 'live' if cfg.live else 'paper',
                'exit_policy': 'profit_floor_after_timer',
                'earliest_exit_seconds': cfg.hold_seconds,
                'paused': store.flag('paused') == '1',
                'unreconciled': store.has_unknowns(),
                'engine_alive': engine_alive,
                'last_poll_age_seconds': round(age, 1) if age is not None else None}

    @app.get('/stats')
    def stats(sid: str = Depends(guard)):
        return store.stats()

    @app.get('/positions')
    def positions(sid: str = Depends(guard)):
        return store.all_positions()

    @app.get('/events')
    def events(sid: str = Depends(guard)):
        return store.events(100)

    # ---------------------------------------------------------------- controls
    def legacy_token(authorization: str | None):
        """Pre-session bearer auth, kept for CLI/script callers."""
        import hmac
        if not cfg.control_token:
            raise HTTPException(403, 'HTTP controls disabled; set CONTROL_TOKEN or sign in')
        if not authorization:
            raise HTTPException(401, 'Authorization required')
        expected = ('Bearer ' + cfg.control_token).encode('utf-8')
        if not hmac.compare_digest(authorization.encode('utf-8', 'surrogateescape'), expected):
            raise HTTPException(401, 'Authorization required')

    def control_allowed(request: Request, authorization: str | None):
        """Either a signed-in session (with CSRF) or the legacy bearer token."""
        if auth.enabled():
            try:
                return auth.authorized(request)
            except HTTPException:
                if not authorization:
                    raise
        legacy_token(authorization)
        return 'token'

    @app.post('/pause')
    def pause(request: Request, authorization: str | None = Header(default=None)):
        control_allowed(request, authorization)
        store.set_flag('paused', '1')
        store.log('OPERATOR_PAUSE')
        auth.audit(request, 'pause', 'ok')
        return {'paused': True}

    @app.post('/resume')
    def resume(request: Request, authorization: str | None = Header(default=None)):
        control_allowed(request, authorization)
        if store.has_unknowns():
            raise HTTPException(409, 'Unreconciled trade; do not resume')
        store.set_flag('paused', '0')
        store.log('OPERATOR_RESUME')
        auth.audit(request, 'resume', 'ok')
        return {'paused': False}

    return app
