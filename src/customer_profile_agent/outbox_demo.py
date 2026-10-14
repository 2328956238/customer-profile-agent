"""2026-10-14 fictional, offline crash/restart demonstration; no model or sending API."""
import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from .agent import ProfileAgent
from .auth import scoped_customer_id
from .mock_provider import EXAMPLES, MockProvider
from .outbox import Dispatcher, FictionalSink, SimulatedCrash
from .profile_store import Store


async def run_demo():
    with TemporaryDirectory(prefix='fictional-outbox-') as directory:
        path = Path(directory)
        cid = scoped_customer_id('demo-shop', 'fictional-customer')
        store = Store(path / 'agent.sqlite3')
        agent = ProfileAgent(store, MockProvider())
        for index, message in enumerate(EXAMPLES[:2], 1):
            result = await agent.get_reply(message, [], cid, {'_message_id': f'm{index}'})
        event_id = result['delivery']['event_id']
        queued = store.get_delivery(cid, event_id)
        sink = FictionalSink(path / 'fictional-receiver.sqlite3', cid)
        now = [time.time() + 1]
        dispatcher = Dispatcher(store, sink, cid, lease_seconds=5, clock=lambda: now[0])
        try:
            dispatcher.dispatch_one(crash_after_send=True)
        except SimulatedCrash:
            pass
        interrupted = store.get_delivery(cid, event_id)
        receipts_after_crash = sink.receipt_count()
        # Advance an injected clock instead of waiting or contacting another system.
        now[0] += 5
        restarted_store = Store(store.path)
        restarted_sink = FictionalSink(sink.path, cid)
        restarted = Dispatcher(restarted_store, restarted_sink, cid,
                               lease_seconds=5, clock=lambda: now[0])
        retry = restarted.dispatch_one()
        final = restarted_store.get_delivery(cid, event_id)
        replay = await ProfileAgent(restarted_store, MockProvider()).get_reply(
            EXAMPLES[1], [], cid, {'_message_id': 'm2'})
        assert queued['status'] == 'pending'
        assert interrupted['status'] == 'inflight' and receipts_after_crash == 1
        assert retry['duplicate_at_sink'] and final['status'] == 'delivered'
        assert final['attempts'] == 2 and restarted_sink.receipt_count() == 1
        assert replay == result and len(restarted_store.list_deliveries(cid)) == 1
        assert restarted_store.get_delivery(scoped_customer_id('other-shop', 'fictional-customer'), event_id) is None
        return {
            'scope': '2026-10-14 脱敏展示版新增；临时数据库、虚构数据、离线接收端',
            'event_id': event_id,
            'after_turn_commit': queued['status'],
            'after_receiver_commit_before_ack': interrupted['status'],
            'after_restart': final['status'],
            'delivery_attempts': final['attempts'],
            'receiver_receipts': restarted_sink.receipt_count(),
            'duplicate_recognized_by_receiver': retry['duplicate_at_sink'],
            'replayed_turn_created_another_event': False,
            'real_external_messages_sent': 0,
            'guarantee': 'At-least-once attempts; receiver deduplicates this stable event ID. Not external exactly-once.',
        }


def main():
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
