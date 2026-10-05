import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from customer_profile_agent.agent import ProfileAgent
from customer_profile_agent.api import create_app
from customer_profile_agent.file_store import FileStore
from customer_profile_agent.mock_provider import EXAMPLES, MockProvider
from customer_profile_agent.profile_store import Conflict, Store, blank, value
from customer_profile_agent.provider import Provider


@pytest.fixture(params=['sqlite', 'files'])
def store(request, tmp_path):
    if request.param == 'sqlite':
        return Store(tmp_path / 'state.sqlite3')
    return FileStore(tmp_path / 'state')


def test_offline_flow_retry_correction_and_restart(store):
    async def run():
        agent = ProfileAgent(store, MockProvider())
        for index, message in enumerate(EXAMPLES, 1):
            result = await agent.get_reply(message, [], 'demo-customer', {'_message_id': f'm{index}'})
            assert not result.get('retryable'), result
        assert result['action'] == 'handoff'
        assert result['external_delivery'] is False
        profile = store.load('demo-customer')
        assert value(profile['items']['item_alpha'], 'qty') == 18
        assert profile['audit'][-1]['old']['value'] == 12
        assert profile['items']['item_alpha']['qty']['quote'] == '18 台'
        assert profile['revision'] == 6
        restarted = type(store)(store.path)
        assert restarted.load('demo-customer') == profile
        retried = await ProfileAgent(restarted, MockProvider()).get_reply(
            EXAMPLES[-1], [], 'demo-customer', {'_message_id': 'm3'})
        assert retried == result
        assert restarted.load('demo-customer')['revision'] == 6
        assert restarted.load('different-customer') == blank()

    # An accidental network dependency in the demo must fail the test.
    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        asyncio.run(run())


def test_store_blocks_running_replays_and_requires_explicit_recovery(store):
    store.begin_turn('c', 'm1', 'fictional request', [])
    with pytest.raises(Conflict):
        store.begin_turn('c', 'm1', 'fictional request', [])
    with pytest.raises(ValueError):
        store.begin_turn('c', 'm1', 'different request', [])
    assert store.recover_turn('c', 'm1') is True
    assert store.recover_turn('c', 'm1') is False
    assert store.begin_turn('c', 'm1', 'fictional request', []) is None
    store.finish_turn('c', 'm1', {'answer': 'saved'})
    assert store.recover_turn('c', 'm1') is False
    assert store.begin_turn('c', 'm1', 'fictional request', []) == {'answer': 'saved'}


def test_concurrent_compare_and_swap_has_one_winner(store):
    def attempt():
        try:
            store.save('c', blank(), expected=0)
            return 'saved'
        except Conflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ['conflict', 'saved']
    assert store.load('c')['revision'] == 1


def test_file_store_uses_opaque_filename_and_private_permissions(tmp_path):
    store = FileStore(tmp_path)
    store.save('../fictional-customer', blank(), 0)
    files = list(tmp_path.glob('*.json'))
    assert len(files) == 1
    assert len(files[0].stem) == 64
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_api_response_replay_and_conflict(tmp_path):
    async def run():
        app = create_app(Store(tmp_path / 'state.db'), MockProvider())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health')).json()['provider'] == 'MockProvider'
            payload = {'message_id': 'm1', 'message': EXAMPLES[0]}
            first = await client.post('/profiles/demo/turns', json=payload)
            assert first.status_code == 200
            assert first.json()['action'] == 'ask'
            second = await client.post('/profiles/demo/turns', json=payload)
            assert second.json() == first.json()
            conflict = await client.post('/profiles/demo/turns', json={**payload, 'message': 'changed'})
            assert conflict.status_code == 409
            invalid = await client.post('/profiles/demo/turns', json={'message_id': '', 'message': 'hello'})
            assert invalid.status_code == 422
            assert (await client.get('/profiles/demo')).json()['profile']['revision'] == 2

    asyncio.run(run())


def test_provider_failure_maps_to_503(tmp_path):
    class UnavailableProvider:
        async def complete(self, messages, tools):
            raise TimeoutError('fictional outage')

    async def run():
        app = create_app(Store(tmp_path / 'state.db'), UnavailableProvider())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/profiles/demo/turns', json={'message_id': 'm1', 'message': 'hello'})
            assert response.status_code == 503
            assert response.json()['answer'] == ''
            assert response.json()['retryable'] is True

    asyncio.run(run())


def test_optional_provider_uses_expected_tool_transport_without_network():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {
            'role': 'assistant', 'content': None, 'tool_calls': []}}]})

    real_client = httpx.AsyncClient
    with patch('customer_profile_agent.provider.httpx.AsyncClient',
               side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
        result = asyncio.run(Provider('https://example.invalid/v1', 'demo-only', 'demo-model').complete(
            [{'role': 'user', 'content': 'fictional'}], []))
    assert result['role'] == 'assistant'
    assert requests[0].url.path == '/v1/chat/completions'
    assert json.loads(requests[0].content)['tool_choice'] == 'required'


def test_live_mode_requires_explicit_credentials(monkeypatch):
    for name in ('PROFILE_BASE_URL', 'PROFILE_API_KEY', 'PROFILE_MODEL'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match='Live mode requires'):
        Provider.configured()
