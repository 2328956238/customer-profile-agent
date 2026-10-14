"""Offline fault and boundary tests for the 2026-10-14 portfolio extension."""
import asyncio
import copy
import json
import multiprocessing
import secrets
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from customer_profile_agent.agent import ProfileAgent
from customer_profile_agent.api import create_app
from customer_profile_agent.auth import ApiAuth, scoped_customer_id
from customer_profile_agent.file_store import FileStore
from customer_profile_agent.mock_provider import EXAMPLES, MockProvider
from customer_profile_agent.outbox import Dispatcher, FictionalSink, SimulatedCrash, event_id_for
from customer_profile_agent.profile_store import Conflict, Store


async def handoff(store, cid='customer'):
    agent = ProfileAgent(store, MockProvider())
    first = await agent.get_reply(EXAMPLES[0], [], cid, {'_message_id': 'm1'})
    assert first['action'] == 'ask'
    assert 'delivery' not in first
    return await agent.get_reply(EXAMPLES[1], [], cid, {'_message_id': 'm2'})


@pytest.fixture
def queued(tmp_path):
    store = Store(tmp_path / 'agent.db')
    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        result = asyncio.run(handoff(store))
    return store, result


def test_validated_handoff_replay_and_restart_queue_one_immutable_snapshot(queued):
    store, result = queued
    assert result['delivery']['event_id'] == event_id_for('customer', 'm2')
    assert result['delivery']['status'] == 'pending'
    assert result['external_delivery'] is False
    assert store.list_deliveries('customer')[0]['attempts'] == 0
    assert len(store.list_deliveries('customer')) == 1
    restarted = Store(store.path)
    replay = asyncio.run(ProfileAgent(restarted, MockProvider()).get_reply(
        EXAMPLES[1], [], 'customer', {'_message_id': 'm2'}))
    assert replay == result
    assert restarted.finish_turn('customer', 'm2', result) == result
    assert len(restarted.list_deliveries('customer')) == 1
    event = restarted.claim_delivery('customer', 'w', now=10**10)
    assert event['payload']['profile'] == result['profile']
    changed = copy.deepcopy(result)
    changed['answer'] = 'different draft'
    with pytest.raises(Conflict, match='immutable'):
        restarted.finish_turn('customer', 'm2', changed)
    with pytest.raises(Conflict, match='immutable'):
        restarted.finish_turn('customer', 'm2', {'answer': ''}, 'failed')


@pytest.mark.parametrize('failure', ['event_insert', 'turn_update'])
def test_sqlite_failure_rolls_back_both_event_and_completion(tmp_path, queued, failure):
    _, result = queued
    store = Store(tmp_path / ('atomic-' + failure + '.db'))
    store.begin_turn('customer', 'm2', EXAMPLES[1], [])
    trigger = ("CREATE TRIGGER injected BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'fault'); END;"
               if failure == 'event_insert' else
               "CREATE TRIGGER injected BEFORE UPDATE ON turns WHEN NEW.status='done' "
               "BEGIN SELECT RAISE(ABORT,'fault'); END;")
    with store.connect() as db:
        db.execute(trigger)
    with pytest.raises(sqlite3.IntegrityError, match='fault'):
        store.finish_turn('customer', 'm2', result)
    with store.connect() as db:
        assert db.execute('SELECT status,result FROM turns').fetchone() == ('running', None)
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
        db.execute('DROP TRIGGER injected')
    # An unchanged retry can complete after the storage fault is removed.
    assert store.finish_turn('customer', 'm2', result) == result
    assert len(store.list_deliveries('customer')) == 1


def test_agent_storage_failure_returns_no_reply_and_no_delivery(tmp_path):
    store = Store(tmp_path / 'failed.db')
    with store.connect() as db:
        db.execute("CREATE TRIGGER injected BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'fault'); END")
    result = asyncio.run(handoff(store))
    assert result['retryable'] is True and result['answer'] == ''
    assert result['send_plan'] == {'split_messages': []}
    assert store.list_deliveries('customer') == []
    assert store.load('customer')['revision'] == 4  # Previously committed facts remain.
    with store.connect() as db:
        assert db.execute("SELECT status FROM turns WHERE mid='m2'").fetchone()[0] == 'failed'


def _process_claim(path):
    return Store(path).claim_delivery('customer', 'process', now=10**10)


def test_atomic_claim_across_processes_has_one_winner(queued):
    store, _ = queued
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as pool:
        outcomes = list(pool.map(_process_claim, [store.path] * 8))
    assert len([event for event in outcomes if event is not None]) == 1
    assert store.list_deliveries('customer')[0]['attempts'] == 1


def test_competing_completions_do_not_duplicate_delivery(tmp_path, queued):
    _, result = queued
    store = Store(tmp_path / 'complete-race.db')
    store.begin_turn('customer', 'm2', EXAMPLES[1], [])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: Store(store.path).finish_turn('customer', 'm2', result), range(8)))
    assert all(saved == result for saved in results)
    assert len(store.list_deliveries('customer')) == 1


def test_expired_claim_and_other_scope_cannot_ack_or_fail_new_owner(queued):
    store, result = queued
    event_id, now = result['delivery']['event_id'], 10**10
    old = store.claim_delivery('customer', 'old', now=now, lease_seconds=5)
    assert not store.acknowledge_delivery('customer', event_id, old['claim_token'], now=now + 5)
    new = store.claim_delivery('customer', 'new', now=now + 5, lease_seconds=5)
    assert new['claim_token'] != old['claim_token']
    assert not store.acknowledge_delivery('customer', event_id, old['claim_token'], now=now + 6)
    assert not store.fail_delivery('customer', event_id, old['claim_token'], now=now + 6, error=RuntimeError())
    assert not store.acknowledge_delivery('other-customer', event_id, new['claim_token'], now=now + 6)
    assert not store.fail_delivery('other-customer', event_id, new['claim_token'], now=now + 6, error=RuntimeError())
    assert store.acknowledge_delivery('customer', event_id, new['claim_token'], now=now + 6)
    assert not store.acknowledge_delivery('customer', event_id, new['claim_token'], now=now + 7)


def test_crash_after_receiver_commit_retries_with_same_id_and_one_receipt(tmp_path, queued):
    store, result = queued
    now = [10**10]
    sink_path = tmp_path / 'receiver.db'
    sink = FictionalSink(sink_path, 'customer')
    dispatcher = Dispatcher(store, sink, 'customer', clock=lambda: now[0], lease_seconds=5)
    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        with pytest.raises(SimulatedCrash):
            dispatcher.dispatch_one(crash_after_send=True)
        assert sink.receipt_count() == 1
        assert store.list_deliveries('customer')[0]['status'] == 'inflight'
        assert dispatcher.dispatch_one()['status'] == 'idle'
        now[0] += 5
        restarted = Dispatcher(Store(store.path), FictionalSink(sink_path, 'customer'),
                               'customer', clock=lambda: now[0], lease_seconds=5)
        receipt = restarted.dispatch_one()
    assert receipt['status'] == 'delivered' and receipt['duplicate_at_sink'] is True
    assert receipt['event_id'] == result['delivery']['event_id']
    assert sink.receipt_count() == 1
    state = store.get_delivery('customer', receipt['event_id'])
    assert state['status'] == 'delivered' and state['attempts'] == 2
    # Cached turn response is a snapshot; the status endpoint is authoritative.
    assert store.begin_turn('customer', 'm2', EXAMPLES[1], [])['delivery']['status'] == 'pending'


def test_failure_backoff_and_attempt_budget_do_not_retry_forever(queued):
    store, result = queued
    now, calls = [10**10], []

    class FailingSink:
        def deliver(self, event):
            calls.append(event['event_id'])
            raise OSError('fictional sensitive text must not enter status')

    dispatcher = Dispatcher(store, FailingSink(), 'customer', clock=lambda: now[0],
                            max_attempts=3, backoff_base=2, backoff_cap=3)
    assert dispatcher.dispatch_one()['status'] == 'pending'
    assert store.list_deliveries('customer')[0]['available_at'] == now[0] + 2
    assert dispatcher.dispatch_one()['status'] == 'idle'
    now[0] += 2
    assert dispatcher.dispatch_one()['status'] == 'pending'
    assert store.list_deliveries('customer')[0]['available_at'] == now[0] + 3
    now[0] += 3
    assert dispatcher.dispatch_one()['status'] == 'dead'
    now[0] += 100
    assert dispatcher.dispatch_one()['status'] == 'idle'
    assert calls == [result['delivery']['event_id']] * 3
    assert store.list_deliveries('customer')[0]['last_error'] == 'OSError'


def test_last_attempt_crash_becomes_dead_with_unknown_delivery_outcome(tmp_path, queued):
    store, _ = queued
    now = [10**10]
    sink = FictionalSink(tmp_path / 'sink.db', 'customer')
    dispatcher = Dispatcher(store, sink, 'customer', max_attempts=1, lease_seconds=1, clock=lambda: now[0])
    with pytest.raises(SimulatedCrash):
        dispatcher.dispatch_one(crash_after_send=True)
    now[0] += 1
    assert dispatcher.dispatch_one()['status'] == 'idle'
    assert store.list_deliveries('customer')[0]['status'] == 'dead'
    assert sink.receipt_count() == 1  # dead cannot be interpreted as "never sent".


def test_bad_receipt_does_not_mark_delivered(queued):
    store, _ = queued

    class WrongReceipt:
        def deliver(self, event):
            return {'event_id': 'another-event'}

    assert Dispatcher(store, WrongReceipt(), 'customer', clock=lambda: 10**10).dispatch_one()['status'] == 'pending'
    assert store.list_deliveries('customer')[0]['delivered_at'] is None


def test_wrong_scope_dispatch_does_not_see_events_and_receiver_rejects_mismatch(tmp_path, queued):
    store, _ = queued
    sink = FictionalSink(tmp_path / 'sink.db', 'other-customer')
    assert Dispatcher(store, sink, 'other-customer', clock=lambda: 10**10).dispatch_one()['status'] == 'idle'
    result = Dispatcher(store, sink, 'customer', clock=lambda: 10**10).dispatch_one()
    assert result['status'] == 'pending'
    assert sink.receipt_count() == 0
    assert store.get_delivery('other-customer', result['event_id']) is None


def test_sink_rejects_same_id_different_payload_and_scope(tmp_path, queued):
    store, _ = queued
    event = store.claim_delivery('customer', 'w', now=10**10)
    sink = FictionalSink(tmp_path / 'sink.db', 'customer')
    assert sink.deliver(event)['duplicate'] is False
    changed = copy.deepcopy(event)
    changed['payload']['summary'] = 'different'
    with pytest.raises(ValueError, match='different payload'):
        sink.deliver(changed)
    other = FictionalSink(sink.path, 'other-customer')
    changed['customer_scope'] = changed['payload']['customer_scope'] = 'other-customer'
    with pytest.raises(ValueError, match='different payload'):
        other.deliver(changed)
    assert sink.receipt_count() == 1 and other.receipt_count() == 0


def test_delivery_api_auth_tenant_and_customer_isolation(tmp_path):
    store = Store(tmp_path / 'api.db')
    tokens = {tenant: secrets.token_urlsafe(32) for tenant in ['a', 'b']}
    app = create_app(store, MockProvider(), ApiAuth(tokens))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            headers_a = {'Authorization': 'Bearer ' + tokens['a']}
            headers_b = {'Authorization': 'Bearer ' + tokens['b'], 'X-Tenant-ID': 'a'}
            for message_id, message in zip(['m1', 'm2'], EXAMPLES):
                response = await client.post('/profiles/shared/turns', headers=headers_a,
                                            json={'message_id': message_id, 'message': message})
                assert response.status_code == 200
            event_id = response.json()['delivery']['event_id']
            path = '/profiles/shared/deliveries'
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path + '/' + event_id)).status_code == 401
            assert len((await client.get(path, headers=headers_a)).json()['items']) == 1
            assert (await client.get(path + '/' + event_id, headers=headers_a)).json()['status'] == 'pending'
            assert (await client.get(path + '?tenant_id=a', headers=headers_b)).json()['items'] == []
            assert (await client.get(path + '/' + event_id, headers=headers_b)).status_code == 404
            assert (await client.get('/profiles/other/deliveries/' + event_id, headers=headers_a)).status_code == 404
            internal_key = scoped_customer_id('a', 'shared')
            assert (await client.get('/profiles/' + internal_key + '/deliveries/' + event_id,
                                     headers=headers_b)).status_code == 404
            # The same external IDs for another authenticated tenant yield another event ID.
            for message_id, message in zip(['m1', 'm2'], EXAMPLES):
                response = await client.post('/profiles/shared/turns', headers=headers_b,
                                            json={'message_id': message_id, 'message': message})
            assert response.json()['delivery']['event_id'] != event_id
            cid = scoped_customer_id('a', 'shared')
            sink = FictionalSink(tmp_path / 'api-sink.db', cid)
            assert Dispatcher(store, sink, cid, clock=lambda: 10**10).dispatch_one()['status'] == 'delivered'
            assert (await client.get(path + '/' + event_id, headers=headers_a)).json()['status'] == 'delivered'
            body = (await client.get(path, headers=headers_a)).text
            assert 'claim_token' not in body and 'profile' not in body
            assert all(token not in body for token in tokens.values())

    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        asyncio.run(run())


def test_file_store_keeps_original_handoff_and_explicitly_has_no_outbox(tmp_path):
    store = FileStore(tmp_path / 'files')
    result = asyncio.run(handoff(store))
    assert result['action'] == 'handoff' and 'delivery' not in result
    with pytest.raises(ValueError, match='requires SQLite'):
        Dispatcher(store, object(), 'customer')
    app = create_app(store, MockProvider(), ApiAuth())

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/profiles/customer/deliveries')).status_code == 501
            assert (await client.get('/profiles/customer/deliveries/anything')).status_code == 501

    asyncio.run(run())
