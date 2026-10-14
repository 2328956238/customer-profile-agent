"""History boundaries and agent integration, using only standard-library mocks."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from customer_profile_agent.agent import ProfileAgent
from customer_profile_agent.file_store import FileStore
from customer_profile_agent.profile_store import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stores = (Store(root / 'state.db'), FileStore(root / 'files'))

    def add(self, store, mid, *, cid='c', created=1.0, answer='answer', status='done'):
        store.begin_turn(cid, mid, 'message ' + mid, [])
        store.finish_turn(cid, mid, {'answer': answer}, status=status)
        if isinstance(store, Store):
            with store.connect() as db:
                db.execute('UPDATE turns SET created=? WHERE cid=? AND mid=?', (created, cid, mid))
        else:
            with store.transaction(cid) as body:
                body['turns'][mid]['created'] = created
                store._write(cid, body)

    def test_default_full_history_and_message_limits(self):
        for store in self.stores:
            with self.subTest(store=type(store).__name__):
                for n in range(5):
                    self.add(store, f'm{n}', created=n)
                full = store.history('c')
                self.assertEqual(len(full), 10)
                self.assertEqual(full, store.history('c', limit=None))
                for limit in (1, 2, 3, 7, 10, 11, 40):
                    self.assertEqual(store.history('c', limit=limit), full[-limit:])
                self.assertEqual(store.history('c', limit=1)[0]['role'], 'assistant')

    def test_zero_limit_does_not_read_storage(self):
        for store in self.stores:
            method = 'connect' if isinstance(store, Store) else 'transaction'
            with patch.object(store, method, side_effect=AssertionError('unexpected I/O')):
                self.assertEqual(store.history('c', limit=0), [])

    def test_negative_and_non_integer_limits_rejected(self):
        for store in self.stores:
            for limit in (-1, -40, True, False, 1.0, '2', [], {}):
                with self.subTest(store=type(store).__name__, limit=limit):
                    with self.assertRaisesRegex(ValueError, 'history limit'):
                        store.history('c', limit=limit)

    def test_only_completed_turns_of_requested_customer(self):
        for store in self.stores:
            self.add(store, 'm1', created=1)
            self.add(store, 'failed', created=100, status='failed')
            self.add(store, 'running', created=101, status='running')
            self.add(store, 'other', cid='other', created=102)
            self.add(store, 'm2', created=2)
            full = store.history('c')
            self.assertEqual([row['message_id'] for row in full if row['role'] == 'user'], ['m1', 'm2'])
            self.assertEqual(store.history('c', limit=3), full[-3:])
            self.assertEqual(store.history('missing', limit=40), [])

    def test_equal_timestamps_use_message_id_in_both_backends(self):
        for store in self.stores:
            for mid in ('m3', 'm1', 'm2'):
                self.add(store, mid, created=10)
            full = store.history('c')
            self.assertEqual([row['message_id'] for row in full if row['role'] == 'user'], ['m1', 'm2', 'm3'])
            self.assertEqual(store.history('c', limit=3), full[-3:])
            self.assertEqual(type(store)(store.path).history('c', limit=3), full[-3:])
        self.assertEqual(self.stores[0].history('c'), self.stores[1].history('c'))

    def test_empty_answers_preserve_each_backends_existing_semantics(self):
        for store in self.stores:
            for n, answer in enumerate(('ok', '', '', 'last', ''), 1):
                self.add(store, f'm{n}', created=n, answer=answer)
            full = store.history('c')
            self.assertEqual(len(full), 10 if isinstance(store, Store) else 7)
            for limit in range(1, 12):
                self.assertEqual(store.history('c', limit=limit), full[-limit:])

    def test_file_store_long_run_of_empty_answers_does_not_lose_older_messages(self):
        store = self.stores[1]
        for n in range(50):
            self.add(store, f'm{n:02}', created=n, answer='')
        limited = store.history('c', limit=40)
        self.assertEqual(len(limited), 40)
        self.assertEqual(limited, store.history('c')[-40:])
        self.assertEqual(limited[0]['message_id'], 'm10')

    def test_sqlite_limited_read_uses_index_and_decodes_only_needed_turns(self):
        store = self.stores[0]
        for n in range(30):
            self.add(store, f'm{n:02}', created=n)
        with patch('customer_profile_agent.profile_store.json.loads', wraps=json.loads) as decode:
            result = store.history('c', limit=3)
        self.assertEqual(len(result), 3)
        self.assertEqual(decode.call_count, 2)
        with store.connect() as db:
            plan = db.execute("EXPLAIN QUERY PLAN SELECT mid,message,result FROM turns "
                              "WHERE cid=? AND status='done' ORDER BY created DESC,mid DESC LIMIT ?",
                              ('c', 2)).fetchall()
        details = ' '.join(row[3] for row in plan)
        self.assertIn('turns_done_history', details)
        self.assertNotIn('TEMP B-TREE', details)

    def test_existing_database_gets_index_without_changing_turns(self):
        store = self.stores[0]
        self.add(store, 'm1')
        expected = store.history('c')
        with store.connect() as db:
            db.execute('DROP INDEX turns_done_history')
        reopened = Store(store.path)
        self.assertEqual(reopened.history('c', limit=40), expected)
        with reopened.connect() as db:
            names = [row[1] for row in db.execute("PRAGMA index_list('turns')")]
        self.assertIn('turns_done_history', names)

    def inspect_agent_context(self, store, history):
        captured = []

        class ProbeProvider:
            async def complete(self, messages, tools):
                captured.append(json.loads(messages[1]['content'])['history'])
                raise RuntimeError('probe complete')

        with self.assertRaisesRegex(RuntimeError, 'probe complete'):
            asyncio.run(ProfileAgent(store, ProbeProvider())._run('current', history, 'c', 'new', []))
        return captured[0]

    def test_agent_requests_only_40_stored_messages_for_empty_history(self):
        for store in self.stores:
            for n in range(25):
                self.add(store, f'm{n:02}', created=n)
            for history in (None, []):
                with patch.object(store, 'history', wraps=store.history) as read:
                    context = self.inspect_agent_context(store, history)
                read.assert_called_once_with('c', limit=40)
                self.assertEqual(len(context), 40)
                self.assertEqual(context[0]['source_id'], 'm05')

    def test_agent_preserves_external_history_and_its_last_40_boundary(self):
        external = [{'role': 'user', 'content': f'external {n}', 'message_id': f'e{n}'} for n in range(45)]
        for store in self.stores:
            with patch.object(store, 'history', side_effect=AssertionError('must use caller history')):
                context = self.inspect_agent_context(store, external)
            self.assertEqual(len(context), 40)
            self.assertEqual(context[0]['source_id'], 'e5')
            self.assertEqual(context[-1]['source_id'], 'e44')
            self.assertEqual(len(external), 45)


if __name__ == '__main__':
    unittest.main()
