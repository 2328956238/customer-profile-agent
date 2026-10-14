"""Local FastAPI adapter. Default operation is offline and needs no API key."""
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .agent import ProfileAgent, view
from .auth import ApiAuth, scoped_customer_id
from .mock_provider import MockProvider
from .profile_store import Conflict, Store
from .provider import Provider


class TurnRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    message: str = Field(min_length=1, max_length=10000)


def create_app(store=None, provider=None, auth: ApiAuth | None = None):
    auth = ApiAuth.from_env() if auth is None else auth
    if store is None:
        store = Store(Path(os.getenv('PROFILE_DB', 'data/profiles.sqlite3')))
    if provider is None:
        mode = os.getenv('PROFILE_MODE', 'mock')
        if mode not in ('mock', 'live'):
            raise RuntimeError('PROFILE_MODE must be mock or live')
        provider = Provider.configured() if mode == 'live' else MockProvider()
    agent = ProfileAgent(store, provider)
    app = FastAPI(title='Customer Profile Agent', version='0.1.0',
                  description='Sanitized local portfolio demo. Offline mock by default.')
    app.state.agent = agent
    bearer = HTTPBearer(auto_error=False)

    def customer_scope(customer_id: str, request: Request,
                       credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        tenant = None
        if auth.enabled:
            if credentials is not None and len(request.headers.getlist('authorization')) == 1:
                tenant = auth.tenant_for_token(credentials.credentials)
            if tenant is None:
                raise HTTPException(status_code=401, detail='Invalid or missing bearer token',
                                    headers={'WWW-Authenticate': 'Bearer'})
        if not 1 <= len(customer_id) <= 100:
            raise HTTPException(status_code=422, detail='customer_id must contain 1–100 characters')
        return scoped_customer_id(tenant, customer_id) if tenant is not None else customer_id

    @app.get('/health')
    def health():
        return {'status': 'ok', 'provider': type(provider).__name__,
                'auth': 'tenant-token' if auth.enabled else 'local-demo-unprotected'}

    @app.get('/profiles/{customer_id}')
    def profile(cid: str = Depends(customer_scope)):
        return view(store.load(cid))

    @app.get('/profiles/{customer_id}/deliveries')
    def deliveries(cid: str = Depends(customer_scope)):
        if not getattr(store, 'supports_outbox', False):
            raise HTTPException(status_code=501, detail='Delivery outbox requires SQLite Store')
        return {'items': store.list_deliveries(cid), 'limit': 50,
                'destination': 'fictional-local-sink'}

    @app.get('/profiles/{customer_id}/deliveries/{event_id}')
    def delivery(event_id: str, cid: str = Depends(customer_scope)):
        if not getattr(store, 'supports_outbox', False):
            raise HTTPException(status_code=501, detail='Delivery outbox requires SQLite Store')
        result = store.get_delivery(cid, event_id)
        if result is None:
            raise HTTPException(status_code=404, detail='Delivery not found')
        return result

    @app.post('/profiles/{customer_id}/turns')
    async def turn(request: TurnRequest, cid: str = Depends(customer_scope)):
        try:
            result = await agent.get_reply(request.message, [], cid,
                                           {'_message_id': request.message_id})
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result.get('retryable'):
            return JSONResponse(status_code=503, content=result)
        return result

    return app
