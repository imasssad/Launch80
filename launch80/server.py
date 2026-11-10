"""Local-only monitoring. Mutating endpoints require a configured long bearer token."""
from __future__ import annotations
from fastapi import FastAPI, Header, HTTPException
from .storage import Store
from .config import Settings

def create_app(store: Store, cfg: Settings) -> FastAPI:
    app = FastAPI(title='Launch80 Monitoring', version='1.0.0', docs_url=None, redoc_url=None)

    @app.get('/health')
    def health():
        return {'ok':True,'mode':'live' if cfg.live else 'paper',
                'exit_policy':'profit_floor_after_timer','earliest_exit_seconds':cfg.hold_seconds,
                'paused':store.flag('paused') == '1','unreconciled':store.has_unknowns()}

    @app.get('/stats')
    def stats():
        return store.stats()

    @app.get('/positions')
    def positions():
        return store.all_positions()

    @app.get('/events')
    def events():
        return store.events(100)

    def auth(authorization):
        import hmac
        if not cfg.control_token:
            raise HTTPException(403,'HTTP controls disabled; set CONTROL_TOKEN or use CLI')
        expected='Bearer '+cfg.control_token
        if not authorization or not hmac.compare_digest(authorization,expected):
            raise HTTPException(401,'Authorization required')

    @app.post('/pause')
    def pause(authorization: str | None = Header(default=None)):
        auth(authorization)
        store.set_flag('paused','1')
        store.log('OPERATOR_PAUSE')
        return {'paused':True}

    @app.post('/resume')
    def resume(authorization: str | None = Header(default=None)):
        auth(authorization)
        if store.has_unknowns():
            raise HTTPException(409,'Unreconciled trade; do not resume')
        store.set_flag('paused','0')
        store.log('OPERATOR_RESUME')
        return {'paused':False}

    return app
