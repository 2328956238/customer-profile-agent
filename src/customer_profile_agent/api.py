"""Local FastAPI adapter. Default operation is offline and needs no API key."""
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent import ProfileAgent, view
from .mock_provider import MockProvider
from .profile_store import Conflict, Store
from .provider import Provider


class TurnRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    message: str = Field(min_length=1, max_length=10000)


def create_app(store=None, provider=None):
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

    @app.get('/health')
    def health():
        return {'status': 'ok', 'provider': type(provider).__name__}

    @app.get('/profiles/{customer_id}')
    def profile(customer_id: str):
        return view(store.load(customer_id))

    @app.post('/profiles/{customer_id}/turns')
    async def turn(customer_id: str, request: TurnRequest):
        if not 1 <= len(customer_id) <= 100:
            raise HTTPException(status_code=422, detail='customer_id must contain 1–100 characters')
        try:
            result = await agent.get_reply(request.message, [], customer_id,
                                           {'_message_id': request.message_id})
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result.get('retryable'):
            return JSONResponse(status_code=503, content=result)
        return result

    return app
