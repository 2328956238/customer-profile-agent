import asyncio
from decimal import Decimal
import json
import secrets
from unittest.mock import patch

import httpx
import pytest

from customer_profile_agent.agent import ProfileAgent
from customer_profile_agent.api import create_app
from customer_profile_agent.concurrency import Admission, CustomerLocks, Overloaded, run_blocking
from customer_profile_agent.lead_scoring import EXAMPLE_FEATURES
from customer_profile_agent.mock_provider import MockProvider, EXAMPLES
from customer_profile_agent.profile_store import Store
from customer_profile_agent.provider import Provider
from customer_profile_agent.security import SecurityStore, Principal
from customer_profile_agent.telemetry import Trace, TraceStore, BudgetExceeded, current_trace


def test_rbac_knowledge_classification_audit_trace_and_revocation(tmp_path):
    store = Store(tmp_path / 'support.db')
    security = SecurityStore(store.path)
    tokens = {name: secrets.token_urlsafe(32) for name in ('admin', 'agent', 'viewer', 'other')}
    for actor, token in tokens.items():
        security.bootstrap_token(token, 'shop-b' if actor == 'other' else 'shop-a', actor,
                                 'admin' if actor == 'other' else actor)
    app = create_app(store, MockProvider(), security=security)
    headers = {name:{'Authorization':'Bearer '+token} for name,token in tokens.items()}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/profiles/demo')).status_code == 401
            doc = {'version':'v1','title':'Shipping guide', 'text':'Standard shipping takes eight days after confirmation.'}
            assert (await client.put('/knowledge/shipping', json=doc, headers=headers['viewer'])).status_code == 403
            assert (await client.put('/knowledge/shipping', json=doc, headers=headers['admin'])).status_code == 200
            read = await client.post('/knowledge/answer', json={'query':'standard shipping'}, headers=headers['viewer'])
            assert read.status_code == 200 and read.json()['status'] == 'answered'
            assert read.json()['evidence'][0]['version'] == 'v1'
            other = await client.post('/knowledge/search?tenant_id=shop-a', json={'query':'standard shipping'},
                                      headers={**headers['other'],'X-Tenant-ID':'shop-a','X-Role':'admin'})
            assert other.json()['status'] == 'abstain'
            payload = {'message_id':'m1','message':EXAMPLES[0]}
            assert (await client.post('/profiles/demo/turns', json=payload, headers=headers['viewer'])).status_code == 403
            response = await client.post('/profiles/demo/turns', json=payload, headers=headers['agent'])
            assert response.status_code == 200
            trace_id = response.headers['X-Trace-ID']
            assert (await client.get('/profiles/demo', headers=headers['other'])).json()['profile']['revision'] == 0
            hist = (await client.get('/profiles/demo/history?limit=1', headers=headers['agent'])).json()
            assert len(hist['items']) == 1
            assert (await client.get('/profiles/demo/history', params={'before':'[NaN,"m"]'}, headers=headers['agent'])).status_code == 422
            score = await client.post('/leads/demo/score', json=EXAMPLE_FEATURES, headers=headers['agent'])
            assert score.status_code == 200 and score.json()['advisory_only'] is True
            forged = await client.post('/leads/demo/score', json={**EXAMPLE_FEATURES,'role':'admin'}, headers=headers['agent'])
            assert forged.status_code == 422
            assert (await client.get('/ops/traces', headers=headers['agent'])).status_code == 403
            traces = (await client.get('/ops/traces', headers=headers['admin'])).json()['items']
            matched = next(x for x in traces if x['trace_id'] == trace_id)
            assert any(s['kind'] == 'tool' for s in matched['usage']['spans'])
            raw = json.dumps(traces)
            assert EXAMPLES[0] not in raw and not any(t in raw for t in tokens.values())
            audit = (await client.get('/ops/audit', headers=headers['admin'])).json()['items']
            assert {'attempt','success','denied'} <= {x['outcome'] for x in audit}
            assert all(x['tenant_id'] == 'shop-a' for x in audit)
            assert (await client.get('/ops/audit', headers=headers['viewer'])).status_code == 403
            security.revoke_token(tokens['agent'])
            assert (await client.get('/profiles/demo', headers=headers['agent'])).status_code == 401
    with patch('socket.socket.connect', side_effect=AssertionError('offline only')):
        asyncio.run(run())


def test_usage_cost_lower_bound_and_unknown_attempts_survive_restart(tmp_path):
    trace = Trace('test', input_rate=Decimal('2'), output_rate=Decimal('4'), max_cost=Decimal('0.001'))
    trace.model(model='fictional', elapsed_ms=1, error='HTTP429')
    trace.model(model='fictional', elapsed_ms=2, usage={'prompt_tokens':1000,'completion_tokens':500})
    summary = trace.summary()
    assert summary['estimated_cost_usd'] is None
    assert Decimal(summary['known_cost_lower_bound_usd']) == Decimal('0.004')
    assert summary['billing_unknown_attempts'] == 1
    with pytest.raises(BudgetExceeded):
        trace.check_budget()
    trace.tool('an unknown function with sensitive text', 1, False)
    assert trace.spans[-1]['tool'] == 'unknown_tool'
    db = TraceStore(tmp_path/'traces.db')
    db.save(trace, Principal('staff','shop-a','admin'), 503, 5)
    assert TraceStore(db.path).page('shop-a')['items'][0]['usage']['estimated_cost_usd'] is None
    assert TraceStore(db.path).page('shop-b')['items'] == []


def test_provider_usage_client_reuse_and_budget_uses_no_network():
    calls, constructions = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'usage':{'prompt_tokens':100,'completion_tokens':20},
                                       'choices':[{'message':{'role':'assistant','tool_calls':[]}}]})
    real = httpx.AsyncClient
    def factory(**kwargs):
        constructions.append(1)
        return real(transport=httpx.MockTransport(respond), **kwargs)
    async def run():
        trace = Trace('test', max_tokens=150, input_rate=Decimal('2'), output_rate=Decimal('4'))
        ctx = current_trace.set(trace)
        provider = Provider('https://example.invalid/v1','fictional-key','fictional-model')
        try:
            await provider.complete([], [])
            assert trace.summary()['input_tokens'] == 100
            assert Decimal(trace.summary()['estimated_cost_usd']) == Decimal('0.00028')
            with pytest.raises(BudgetExceeded):
                await provider.complete([], [])
            assert len(constructions) == 1 and len(calls) == 2
            assert json.loads(calls[0].content)['max_tokens'] == 2048
        finally:
            await provider.aclose()
            current_trace.reset(ctx)
    with patch('customer_profile_agent.provider.httpx.AsyncClient', side_effect=factory):
        asyncio.run(run())


def test_bounded_admission_isolated_tenant_and_cancel_cleanup():
    async def run():
        admission = Admission(2, 1)
        async with admission.enter('a'):
            with pytest.raises(Overloaded):
                async with admission.enter('a'):
                    pass
            async with admission.enter('b'):
                with pytest.raises(Overloaded):
                    async with admission.enter('c'):
                        pass
        assert admission.active == 0 and admission.tenants == {}
        entered = asyncio.Event()
        async def held():
            async with admission.enter('a'):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(held())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admission.active == 0 and admission.tenants == {}
    asyncio.run(run())


def test_lock_wait_timeout_reclaims_waiters_and_does_not_begin_turn(tmp_path):
    async def run():
        agent = ProfileAgent(Store(tmp_path/'state.db'), MockProvider(), timeout=0.01)
        async with agent._customer_locks.hold('c', 1):
            result = await agent.get_reply(EXAMPLES[0], [], 'c', {'_message_id':'m1'})
            assert result['issues'] == ['LockTimeout']
            assert agent.store.load('c')['revision'] == 0
        assert agent.locks == {}
        result = await agent.get_reply(EXAMPLES[0], [], 'c', {'_message_id':'m1'})
        assert result['action'] == 'ask' and agent.locks == {}
    asyncio.run(run())


def test_api_overload_does_not_block_health_or_start_rejected_message(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow(MockProvider):
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return await super().complete(messages, tools)
    store = Store(tmp_path/'state.db')
    app = create_app(store, Slow(), admission=Admission(1,1))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            task = asyncio.create_task(client.post('/profiles/a/turns',json={'message_id':'m1','message':EXAMPLES[0]}))
            await entered.wait()
            rejected = await client.post('/profiles/b/turns',json={'message_id':'m1','message':EXAMPLES[0]})
            assert rejected.status_code == 429 and rejected.headers['Retry-After'] == '1'
            assert (await client.get('/health')).status_code == 200
            release.set()
            assert (await task).status_code == 200
        assert app.state.admission.active == 0 and app.state.agent.locks == {}
    asyncio.run(run())
    assert store.load('b')['revision'] == 0


def test_keyset_pagination_equal_timestamps_new_insert_and_query_plan(tmp_path):
    store = Store(tmp_path/'state.db')
    for mid in ['a','b','c','d']:
        store.begin_turn('c', mid, mid, [])
        store.finish_turn('c', mid, {'answer':mid})
    with store.connect() as db:
        db.execute('UPDATE turns SET created=1')
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        plan = db.execute("EXPLAIN QUERY PLAN SELECT mid FROM turns WHERE cid=? AND status='done' "
                          'AND (created,mid)<(?,?) ORDER BY created DESC,mid DESC LIMIT 2',('c',1,'d')).fetchall()
        assert any('turns_done_history' in str(row) for row in plan)
    first = store.turn_page('c',2)
    assert [x['message_id'] for x in first['items']] == ['d','c']
    store.begin_turn('c','new','new',[])
    store.finish_turn('c','new',{'answer':'new'})
    second = store.turn_page('c',2,first['next_cursor'])
    assert [x['message_id'] for x in second['items']] == ['b','a']
    assert second['next_cursor'] is None and store.turn_page('other')['items'] == []


def test_cancelled_blocking_work_keeps_capacity_until_thread_exits():
    import threading
    release = threading.Event()
    async def run():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        admission = Admission(1,1)
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
        async def owner():
            async with admission.enter('a'):
                await run_blocking(blocked)
        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        try:
            with pytest.raises(Overloaded):
                async with admission.enter('b'):
                    pass
            assert admission.active == 1
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admission.active == 0
    asyncio.run(run())


@pytest.mark.parametrize('mode',['cancelled','invalid-json'])
def test_inflight_provider_failure_is_unknown_cost_not_zero(mode):
    async def run():
        entered = asyncio.Event()
        async def respond(request):
            entered.set()
            if mode == 'cancelled':
                await asyncio.Event().wait()
            return httpx.Response(200,content=b'not json')
        real = httpx.AsyncClient
        trace = Trace('test')
        context = current_trace.set(trace)
        with patch('customer_profile_agent.provider.httpx.AsyncClient',
                   side_effect=lambda **kw: real(transport=httpx.MockTransport(respond),**kw)):
            provider = Provider('https://example.invalid/v1','not-a-real-key','fictional')
            try:
                task = asyncio.create_task(provider.complete([],[]))
                await entered.wait()
                if mode == 'cancelled':
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(ValueError):
                        await task
            finally:
                await provider.aclose()
                current_trace.reset(context)
        assert trace.summary()['model_attempts'] == 1
        assert trace.summary()['usage_complete'] is False
        assert trace.summary()['estimated_cost_usd'] is None
    asyncio.run(run())


def test_huge_pagination_values_are_rejected_before_sqlite(tmp_path):
    store = Store(tmp_path/'state.db')
    with pytest.raises(ValueError):
        store.turn_page('demo',before=[10**400,'m'])
    security = SecurityStore(store.path)
    with pytest.raises(ValueError):
        security.list_audit('shop',before_id=10**100)
    with pytest.raises(ValueError):
        TraceStore(store.path).page('shop',before_id=10**100)


def test_grounded_model_adapter_integrates_knowledge_and_metering_without_network(tmp_path,monkeypatch):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        evidence = json.loads(body['messages'][1]['content'])['evidence'][0]
        content = json.dumps({'answer':evidence['text'],'citations':[evidence['citation_id']]})
        return httpx.Response(200,json={'usage':{'prompt_tokens':120,'completion_tokens':30},
                              'choices':[{'message':{'role':'assistant','content':content}}]})
    monkeypatch.setenv('PROFILE_INPUT_USD_PER_MILLION','2')
    monkeypatch.setenv('PROFILE_OUTPUT_USD_PER_MILLION','4')
    real = httpx.AsyncClient
    async def run():
        model = Provider('https://example.invalid/v1','fictional-key','fictional-model')
        app = create_app(Store(tmp_path/'support.db'),MockProvider(),knowledge_provider=model)
        app.state.knowledge.upsert('local-demo','shipping','v1','Shipping','Standard shipping takes eight days.')
        async with real(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
            response = await client.post('/knowledge/answer',json={'query':'standard shipping'})
            assert response.json()['status'] == 'answered'
            assert response.json()['answer'] == 'Standard shipping takes eight days.'
            traces = (await client.get('/ops/traces')).json()['items']
            trace = next(r for r in traces if r['trace_id']==response.headers['X-Trace-ID'])
            assert trace['usage']['model_attempts'] == 1
            assert trace['usage']['input_tokens'] == 120
            assert any(span['kind']=='retrieval' for span in trace['usage']['spans'])
        await model.aclose()
    with patch('customer_profile_agent.provider.httpx.AsyncClient',
               side_effect=lambda **kw: real(transport=httpx.MockTransport(respond),**kw)):
        asyncio.run(run())
    assert requests[0]['response_format'] == {'type':'json_object'}
    assert 'tools' not in requests[0]
