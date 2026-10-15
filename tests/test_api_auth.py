"""Local-only contract tests for bearer authentication and tenant boundaries."""
import asyncio
import json
import secrets
from unittest.mock import patch

import httpx
import pytest

from customer_profile_agent.api import create_app
from customer_profile_agent.auth import ApiAuth, scoped_customer_id
from customer_profile_agent.file_store import FileStore
from customer_profile_agent.mock_provider import EXAMPLES, MockProvider
from customer_profile_agent.profile_store import Store, blank


@pytest.fixture(params=['sqlite', 'files'])
def store(request, tmp_path):
    return Store(tmp_path / 'state.db') if request.param == 'sqlite' else FileStore(tmp_path / 'state')


class RecordingProvider(MockProvider):
    def __init__(self):
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        await asyncio.sleep(0)  # Let concurrent requests interleave at a real await.
        return await super().complete(messages, tools)


@pytest.mark.parametrize('case', ['missing', 'invalid', 'basic', 'empty', 'duplicate'])
def test_all_profile_routes_require_one_valid_bearer_token(store, case, caplog):
    token = secrets.token_urlsafe(32)
    headers = {
        'missing': {}, 'invalid': {'Authorization': 'Bearer ' + secrets.token_urlsafe(32)},
        'basic': {'Authorization': 'Basic ' + token}, 'empty': {'Authorization': 'Bearer'},
        'duplicate': [('Authorization', 'Bearer ' + token), ('Authorization', 'Bearer ' + token)],
    }[case]
    provider = RecordingProvider()
    app = create_app(store, provider, ApiAuth({'tenant-a': token}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for method, path, body in [
                ('GET', '/profiles/shared', None),
                ('POST', '/profiles/shared/turns', {'message_id': 'm1', 'message': EXAMPLES[0]}),
            ]:
                response = await client.request(method, path, headers=headers, json=body)
                assert response.status_code == 401
                assert response.headers['www-authenticate'] == 'Bearer'
                assert response.json() == {'detail': 'Invalid or missing bearer token'}
                assert token not in response.text
            assert (await client.get('/health')).json()['auth'] == 'tenant-token'

    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        asyncio.run(run())
    assert provider.messages == []
    assert store.load(scoped_customer_id('tenant-a', 'shared')) == blank()
    assert token not in caplog.text
    assert token not in repr(ApiAuth({'tenant-a': token}))


def test_tenants_cannot_read_write_or_replay_each_others_customer(store, caplog):
    tokens = {tenant: secrets.token_urlsafe(32) for tenant in ['tenant-a', 'tenant-b']}
    provider = RecordingProvider()
    app = create_app(store, provider, ApiAuth(tokens))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            headers_a = {'Authorization': 'Bearer ' + tokens['tenant-a']}
            headers_b = {'Authorization': 'Bearer ' + tokens['tenant-b'], 'X-Tenant-ID': 'tenant-a'}
            payload_a = {'message_id': 'm1', 'message': EXAMPLES[0]}
            first = await client.post('/profiles/shared/turns', json=payload_a, headers=headers_a)
            assert first.status_code == 200
            read_b = await client.get('/profiles/shared?tenant_id=tenant-a', headers=headers_b)
            assert read_b.json()['profile'] == blank()
            # Knowing the other tenant's exact internal key does not grant access.
            raw_key = scoped_customer_id('tenant-a', 'shared')
            assert (await client.get('/profiles/' + raw_key, headers=headers_b)).json()['profile'] == blank()
            # Same external customer + message IDs, different tenant and content.
            second = await client.post('/profiles/shared/turns?tenant_id=tenant-a',
                json={'message_id': 'm1', 'message': 'fictional tenant B only', 'tenant_id': 'tenant-a'},
                headers=headers_b)
            assert second.status_code == 200
            assert second.json()['profile']['items'] == {}
            assert first.json()['profile']['items']['item_alpha']['model']['value'] == 'Device Alpha'
            before_replay = len(provider.messages)
            replay = await client.post('/profiles/shared/turns', json=payload_a, headers=headers_a)
            assert replay.json() == first.json()
            assert len(provider.messages) == before_replay
            read_a = await client.get('/profiles/shared', headers=headers_a)
            assert read_a.json()['profile'] == first.json()['profile']
            conflict = await client.post('/profiles/shared/turns', headers=headers_a,
                json={'message_id': 'm1', 'message': 'changed'})
            assert conflict.status_code == 409

    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        asyncio.run(run())
    assert store.load('shared') == blank()
    history_a = store.history(scoped_customer_id('tenant-a', 'shared'))
    history_b = store.history(scoped_customer_id('tenant-b', 'shared'))
    assert history_a[0]['content'] == EXAMPLES[0]
    assert history_b[0]['content'] == 'fictional tenant B only'
    for token in tokens.values():
        assert token not in json.dumps(provider.messages)
        assert token not in json.dumps(history_a + history_b)
        assert token not in caplog.text


def test_concurrent_tenant_pairs_have_distinct_storage_locks_and_history(store):
    # These tuples would collide if joined with a plain ':' delimiter.
    pairs = [('team:west', 'client'), ('team', 'west:client')]
    tokens = {tenant: secrets.token_urlsafe(32) for tenant, _ in pairs}
    app = create_app(store, RecordingProvider(), ApiAuth(tokens))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            responses = await asyncio.gather(*[
                client.post(f'/profiles/{customer}/turns',
                    headers={'Authorization': 'Bearer ' + tokens[tenant]},
                    json={'message_id': 'same-id', 'message': message})
                for (tenant, customer), message in zip(pairs, [EXAMPLES[0], 'fictional other tenant'])])
            assert [response.status_code for response in responses] == [200, 200]
            assert responses[0].json()['profile']['items']
            assert not responses[1].json()['profile']['items']

    asyncio.run(run())
    keys = [scoped_customer_id(*pair) for pair in pairs]
    assert len(set(keys)) == 2
    # Customer isolation was checked above; idle locks are now reclaimed.
    assert app.state.agent.locks == {}
    assert [store.load(key)['revision'] for key in keys] == [2, 2]
    assert [store.history(key)[0]['content'] for key in keys] == [EXAMPLES[0], 'fictional other tenant']


def test_namespace_handles_unicode_delimiters_and_embedded_namespace():
    pairs = [('中文', '客户'), ('a:b', 'c'), ('a', 'b:c'),
             ('a', '["a","b"]'), ('a', scoped_customer_id('a', 'b'))]
    encoded = [scoped_customer_id(*pair) for pair in pairs]
    assert len(set(encoded)) == len(pairs)


@pytest.mark.parametrize('raw', ['', '{}', 'null', '[]', 'oops', '{"a":"short"}',
                                  '{"a":12}', '{"": "' + 'x' * 32 + '"}',
                                  '{"a":"' + 'x' * 32 + '","a":"' + 'y' * 32 + '"}',
                                  '{"a":"' + 'x' * 32 + '","b":"' + 'x' * 32 + '"}'])
def test_explicit_invalid_env_configuration_fails_closed_without_data_store(monkeypatch, raw):
    monkeypatch.setenv('PROFILE_AUTH_TOKENS', raw)
    with patch('customer_profile_agent.api.Store') as storage:
        with pytest.raises(ValueError, match='Invalid PROFILE_AUTH_TOKENS') as error:
            create_app(provider=MockProvider())
        storage.assert_not_called()
    assert 'x' * 32 not in str(error.value)


@pytest.mark.parametrize('mapping', [{}, {'a': 'short'}, {'a': 'x' * 257},
    {'a': 'x' * 31 + ' '}, {'a': '密' * 32}, {12: 'x' * 32},
    {'a': 'x' * 32, 'b': 'x' * 32}])
def test_invalid_injected_configuration_is_rejected(mapping):
    with pytest.raises(ValueError, match='PROFILE_AUTH_TOKENS'):
        ApiAuth(mapping)


def test_valid_env_configuration_and_default_demo_behavior(monkeypatch, tmp_path):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv('PROFILE_AUTH_TOKENS', json.dumps({'demo-tenant': token}))
    auth = ApiAuth.from_env()
    assert auth.enabled and auth.tenant_for_token(token) == 'demo-tenant'
    monkeypatch.delenv('PROFILE_AUTH_TOKENS')
    assert not ApiAuth.from_env().enabled

    async def run():
        app = create_app(Store(tmp_path / 'demo.db'), MockProvider())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health')).json()['auth'] == 'local-demo-unprotected'
            response = await client.post('/profiles/demo/turns',
                json={'message_id': 'm1', 'message': EXAMPLES[0]})
            assert response.status_code == 200
            assert (await client.get('/profiles/demo')).json()['profile']['revision'] == 2
            assert (await client.get('/profiles/' + 'x' * 101)).status_code == 422

    asyncio.run(run())


def test_env_enabled_app_and_restart_token_rotation_preserve_tenant_data(monkeypatch, tmp_path):
    store = Store(tmp_path / 'rotation.db')
    original = secrets.token_urlsafe(32)
    replacement = secrets.token_urlsafe(32)
    monkeypatch.setenv('PROFILE_AUTH_TOKENS', json.dumps({'shop': original}))
    app = create_app(store, MockProvider())

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/profiles/shared')).status_code == 401
            saved = await client.post('/profiles/shared/turns',
                headers={'Authorization': 'Bearer ' + original},
                json={'message_id': 'm1', 'message': EXAMPLES[0]})
            assert saved.status_code == 200
        monkeypatch.setenv('PROFILE_AUTH_TOKENS', json.dumps({'shop': replacement}))
        restarted = create_app(Store(store.path), MockProvider())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://test') as client:
            assert (await client.get('/profiles/shared',
                headers={'Authorization': 'Bearer ' + original})).status_code == 401
            response = await client.get('/profiles/shared',
                headers={'Authorization': 'Bearer ' + replacement})
            assert response.status_code == 200
            assert response.json()['profile'] == saved.json()['profile']

    asyncio.run(run())
