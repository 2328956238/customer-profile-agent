"""Local support API: synthetic data, offline provider, optional individual RBAC."""
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from .agent import ProfileAgent, view
from .auth import ApiAuth, scoped_customer_id
from .concurrency import Admission, Overloaded, run_blocking
from .knowledge import KnowledgeBase, KnowledgeConflict
from .mock_provider import MockProvider
from .profile_store import Conflict, Store
from .provider import Provider
from .security import Principal, SecurityStore, require
from .telemetry import Trace, TraceStore, current_trace

class TurnRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    message: str = Field(min_length=1, max_length=10000)

class KnowledgeDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100000)
    expected_version: str | None = Field(default=None, max_length=80)

class KnowledgeQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)

class LeadFeatures(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_count: int = Field(ge=1, le=200, strict=True)
    specified_attribute_count: int = Field(ge=0, le=4, strict=True)
    requested_quantity: int = Field(ge=0, le=1000000, strict=True)
    has_destination: bool = Field(strict=True)
    asks_price: bool = Field(strict=True)
    asks_shipping: bool = Field(strict=True)
    has_purchase_timeframe: bool = Field(strict=True)

@lru_cache(maxsize=1)
def demo_scorer():
    from .lead_scoring import train_demo_model
    return train_demo_model()

def create_app(store=None, provider=None, auth=None, *, security=None, knowledge=None, scorer=None, admission=None):
    auth = ApiAuth.from_env() if auth is None else auth
    mode = os.getenv('PROFILE_ACCESS_MODE', 'legacy')
    if mode not in ('legacy', 'rbac'):
        raise RuntimeError('PROFILE_ACCESS_MODE must be legacy or rbac')
    if (security is not None or mode == 'rbac') and auth.enabled:
        raise RuntimeError('Choose individual RBAC tokens or legacy tenant tokens, not both')
    store = store if store is not None else Store(Path(os.getenv('PROFILE_DB', 'data/profiles.sqlite3')))
    support_db = store.path if getattr(store, 'supports_outbox', False) else store.path / 'support.sqlite3'
    security_store = security if security is not None else SecurityStore(support_db)
    rbac = security is not None or mode == 'rbac'
    knowledge = knowledge if knowledge is not None else KnowledgeBase(support_db)
    traces = TraceStore(support_db)
    admission = admission or Admission(int(os.getenv('PROFILE_GLOBAL_CONCURRENCY', '8')),
                                       int(os.getenv('PROFILE_TENANT_CONCURRENCY', '4')))
    if provider is None:
        provider_mode = os.getenv('PROFILE_MODE', 'mock')
        if provider_mode not in ('mock', 'live'):
            raise RuntimeError('PROFILE_MODE must be mock or live')
        provider = Provider.configured() if provider_mode == 'live' else MockProvider()
    agent = ProfileAgent(store, provider)
    options = {'max_tokens': int(os.getenv('PROFILE_TOKEN_BUDGET', '12000')),
               'max_attempts': int(os.getenv('PROFILE_MODEL_ATTEMPTS', '20')),
               'pricing_revision': os.getenv('PROFILE_PRICING_REVISION', 'unconfigured')[:100]}
    for env, key in [('PROFILE_INPUT_USD_PER_MILLION','input_rate'),
                     ('PROFILE_OUTPUT_USD_PER_MILLION','output_rate'), ('PROFILE_COST_BUDGET_USD','max_cost')]:
        if env in os.environ:
            options[key] = Decimal(os.environ[env])
    Trace('configuration-check', **options)
    if ('input_rate' in options) != ('output_rate' in options):
        raise RuntimeError('Configure both input and output prices, or neither')
    if 'max_cost' in options and 'input_rate' not in options:
        raise RuntimeError('Cost budget requires explicit input/output prices')

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if hasattr(provider, 'aclose'):
                await provider.aclose()
    app = FastAPI(title='Customer Support Engineering Demo', version='0.2.0', lifespan=lifespan,
                  description='Synthetic support demo; RBAC, knowledge, classification and traces. Offline by default.')
    app.state.agent, app.state.security, app.state.knowledge = agent, security_store, knowledge
    app.state.traces, app.state.admission = traces, admission
    bearer = HTTPBearer(auto_error=False)

    @app.middleware('http')
    async def observe(request, call_next):
        trace = Trace('http', **options)
        context = current_trace.set(trace)
        started, status = time.monotonic(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers['X-Trace-ID'] = trace.trace_id
            return response
        finally:
            current_trace.reset(context)
            principal = getattr(request.state, 'principal', None)
            if principal is not None:
                route = request.scope.get('route')
                trace.operation = request.method + ' ' + (getattr(route, 'path', None) or 'unmatched')
                await asyncio.to_thread(traces.save, trace, principal, status,
                                        round((time.monotonic()-started)*1000, 3))

    def identity(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = credentials.credentials if credentials and len(request.headers.getlist('authorization')) == 1 else None
        if rbac:
            principal = security_store.authenticate(token)
        elif auth.enabled:
            tenant = auth.tenant_for_token(token) if token is not None else None
            principal = Principal('legacy-token', tenant, 'agent') if tenant is not None else None
        else:
            principal = Principal('local-demo', 'local-demo', 'admin')
        if principal is None:
            raise HTTPException(401, 'Invalid or missing bearer token', headers={'WWW-Authenticate':'Bearer'})
        request.state.principal = principal
        return principal

    def permission(name):
        def check(principal: Principal = Depends(identity)):
            try:
                require(principal, name)
            except PermissionError as exc:
                trace = current_trace.get()
                security_store.record_audit(principal, name, 'permission', 'denied',
                                            {'reason':'permission_denied', 'trace_id':trace.trace_id if trace else None})
                raise HTTPException(403, 'Permission denied') from exc
            return principal
        return check

    def cid_for(customer_id, principal):
        if not 1 <= len(customer_id) <= 100:
            raise HTTPException(422, 'customer_id must contain 1–100 characters')
        return scoped_customer_id(principal.tenant_id, customer_id) if rbac or auth.enabled else customer_id

    def audit(principal, action, resource, outcome, **details):
        opaque = 'resource:' + hashlib.sha256(resource.encode()).hexdigest()
        trace = current_trace.get()
        if trace:
            details['trace_id'] = trace.trace_id
        security_store.record_audit(principal, action, opaque, outcome, details)

    @app.exception_handler(Overloaded)
    async def overloaded(request, exc):
        return JSONResponse(status_code=429, content={'detail':str(exc), 'retryable':True}, headers={'Retry-After':'1'})

    @app.exception_handler(KnowledgeConflict)
    async def knowledge_conflict(request, exc):
        return JSONResponse(status_code=409, content={'detail':str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_input(request, exc):
        return JSONResponse(status_code=422, content={'detail':'Invalid operation input'})

    @app.get('/health')
    def health():
        return {'status':'ok', 'provider':type(provider).__name__,
                'auth':'individual-rbac' if rbac else 'tenant-token' if auth.enabled else 'local-demo-unprotected'}

    @app.get('/profiles/{customer_id}')
    def profile(customer_id: str, principal=Depends(permission('profile:read'))):
        return view(store.load(cid_for(customer_id, principal)))

    @app.get('/profiles/{customer_id}/history')
    def history(customer_id: str, limit: int = Query(20, ge=1, le=100), before: str | None = None,
                principal=Depends(permission('profile:read'))):
        if not hasattr(store, 'turn_page'):
            raise HTTPException(501, 'Keyset history requires SQLite Store')
        try:
            cursor = json.loads(before) if before is not None else None
            return store.turn_page(cid_for(customer_id, principal), limit, cursor)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, 'Invalid history cursor') from exc

    @app.get('/profiles/{customer_id}/deliveries')
    def deliveries(customer_id: str, principal=Depends(permission('profile:read'))):
        if not getattr(store, 'supports_outbox', False):
            raise HTTPException(501, 'Delivery outbox requires SQLite Store')
        return {'items':store.list_deliveries(cid_for(customer_id, principal)), 'limit':50,
                'destination':'fictional-local-sink'}

    @app.get('/profiles/{customer_id}/deliveries/{event_id}')
    def delivery(customer_id: str, event_id: str, principal=Depends(permission('profile:read'))):
        if not getattr(store, 'supports_outbox', False):
            raise HTTPException(501, 'Delivery outbox requires SQLite Store')
        result = store.get_delivery(cid_for(customer_id, principal), event_id)
        if result is None:
            raise HTTPException(404, 'Delivery not found')
        return result

    @app.post('/profiles/{customer_id}/turns')
    async def turn(customer_id: str, request: TurnRequest, principal=Depends(permission('profile:write'))):
        cid = cid_for(customer_id, principal)
        async with admission.enter(principal.tenant_id):
            await asyncio.to_thread(audit, principal, 'profile:write', cid, 'attempt')
            try:
                result = await agent.get_reply(request.message, [], cid, {'_message_id':request.message_id})
            except (Conflict, ValueError) as exc:
                await asyncio.to_thread(audit, principal, 'profile:write', cid, 'failed', reason='conflict')
                raise HTTPException(409, str(exc)) from exc
            await asyncio.to_thread(audit, principal, 'profile:write', cid,
                                    'failed' if result.get('retryable') else 'success',
                                    revision=result.get('state_revision', 0))
            return JSONResponse(status_code=503 if result.get('retryable') else 200, content=result)

    @app.put('/knowledge/{doc_id}')
    def put_document(doc_id: str, body: KnowledgeDocument, principal=Depends(permission('knowledge:write'))):
        audit(principal, 'knowledge:write', doc_id, 'attempt')
        try:
            result = knowledge.upsert(principal.tenant_id, doc_id, body.version, body.title,
                                      body.text, expected_version=body.expected_version)
        except (ValueError, KnowledgeConflict):
            audit(principal, 'knowledge:write', doc_id, 'failed')
            raise
        audit(principal, 'knowledge:write', doc_id, 'success')
        return result

    @app.delete('/knowledge/{doc_id}')
    def delete_document(doc_id: str, principal=Depends(permission('knowledge:write'))):
        audit(principal, 'knowledge:write', doc_id, 'attempt')
        changed = knowledge.delete(principal.tenant_id, doc_id)
        audit(principal, 'knowledge:write', doc_id, 'success', changed=changed)
        return {'deleted':changed}

    @app.post('/knowledge/search')
    async def knowledge_search(body: KnowledgeQuestion, principal=Depends(permission('knowledge:read'))):
        async with admission.enter(principal.tenant_id):
            return await run_blocking(knowledge.search, principal.tenant_id, body.query)

    @app.post('/knowledge/answer')
    async def knowledge_answer(body: KnowledgeQuestion, principal=Depends(permission('knowledge:read'))):
        async with admission.enter(principal.tenant_id):
            return await run_blocking(knowledge.extractive_answer, principal.tenant_id, body.query)

    @app.post('/leads/{customer_id}/score')
    async def lead_score(customer_id: str, body: LeadFeatures, principal=Depends(permission('lead:score'))):
        cid_for(customer_id, principal)
        async with admission.enter(principal.tenant_id):
            selected = scorer or (await run_blocking(demo_scorer))[0]
            result = await run_blocking(selected.predict, body.model_dump())
            await asyncio.to_thread(audit, principal, 'lead:score', customer_id, 'success')
            return result

    @app.get('/ops/traces')
    def trace_page(limit: int = Query(50, ge=1, le=100), before_id: int | None = Query(None, gt=0, le=9223372036854775807),
                   principal=Depends(permission('metrics:read'))):
        return traces.page(principal.tenant_id, limit, before_id)

    @app.get('/ops/audit')
    def audit_page(limit: int = Query(50, ge=1, le=100), before_id: int | None = Query(None, gt=0, le=9223372036854775807),
                   principal=Depends(permission('audit:read'))):
        return {'items':security_store.list_audit(principal.tenant_id, limit, before_id)}
    return app
