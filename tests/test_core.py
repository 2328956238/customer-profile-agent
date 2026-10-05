import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from customer_profile_agent.agent import ProfileAgent, validate_reply, questions
from customer_profile_agent.profile_store import Store, Conflict, blank, apply_patch, value
from customer_profile_agent.policy import requirements


def op(field, val, target='item_one', quote='customer evidence', status='confirmed'):
    return dict(operation='set', target=target, field=field, value=val,
                status=status, source_id='m1', quote=quote)


class ProfileTests(unittest.TestCase):
    def test_french_storage_units_preserve_source(self):
        for raw, expected in [('128 Go', '128GB'), ('1 To', '1TB')]:
            p = apply_patch(blank(), [op('storage', raw, quote=raw)], {'m1': raw})
            self.assertEqual(value(p['items']['item_one'], 'storage'), expected)
            self.assertEqual(p['items']['item_one']['storage']['quote'], raw)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.db')
        self.sources = {'m1': 'customer evidence'}

    def test_atomic_patch_rejects_invented_evidence(self):
        initial = blank()
        with self.assertRaises(ValueError):
            apply_patch(initial, [op('grade', 'A+'), op('qty', 5, quote='made up')], self.sources)
        self.assertEqual(initial, blank())

    def test_incremental_amendment_keeps_specs_and_audit(self):
        a = apply_patch(blank(), [op('grade', 'A+'), op('qty', 5), op('storage', '128GB')], self.sources)
        a = self.store.save('c', a, 0)
        b = apply_patch(a, [op('qty', 10)], self.sources)
        b = self.store.save('c', b, 1)
        self.assertEqual(value(b['items']['item_one'], 'grade'), 'A+')
        self.assertEqual(value(b['items']['item_one'], 'storage'), '128GB')
        self.assertEqual(b['audit'][-1]['old']['value'], 5)
        self.assertEqual(value(a['items']['item_one'], 'qty'), 5)

    def test_stale_revision_cannot_overwrite_another_message(self):
        a = self.store.load('c')
        b = self.store.load('c')
        self.store.save('c', apply_patch(a, [op('grade', 'A+')], self.sources), 0)
        with self.assertRaises(Conflict):
            self.store.save('c', apply_patch(b, [op('qty', 10)], self.sources), 0)
        self.assertEqual(value(self.store.load('c')['items']['item_one'], 'grade'), 'A+')

    def test_restart_and_customer_isolation(self):
        self.store.save('c', apply_patch(blank(), [op('grade', 'A+')], self.sources), 0)
        restarted = Store(self.store.path)
        self.assertEqual(value(restarted.load('c')['items']['item_one'], 'grade'), 'A+')
        self.assertEqual(restarted.load('other'), blank())

    def test_total_not_silently_allocated(self):
        a = apply_patch(blank(), [op('model', 'Device Alpha'),
            op('model', 'Device Beta', 'item_two'),
            op('quantity_mentioned', 5, 'order', status='ambiguous')], self.sources)
        self.assertFalse(any('qty' in i for i in a['items'].values()))
        with self.assertRaises(ValueError):
            validate_reply(a, dict(revision=0, action='ask', targets=['item_one'], field='qty', answer='How many?'), '')
        validate_reply(a, dict(revision=0, action='clarify', targets=['order'], field='quantity_mentioned', answer='5 total or each?'), '')
        options = questions(a)
        self.assertFalse(any(q['field'] == 'qty' for q in options))
        self.assertEqual(next(q['targets'] for q in options if q['field'] == 'quantity_mentioned'), ['order'])

    def test_cannot_reask_confirmed_grade(self):
        a = apply_patch(blank(), [op('grade', 'A+')], self.sources)
        for action in ('ask', 'clarify'):
            with self.assertRaises(ValueError):
                validate_reply(a, dict(revision=0, action=action, targets=['item_one'], field='grade', answer='Which grade?'), '')

    def test_no_handoff_for_incomplete_order(self):
        with self.assertRaises(ValueError):
            validate_reply(blank(), dict(revision=0, action='handoff', targets=[], field='', answer='Ready.', confirmation_quote='yes'), 'yes')

    def test_duplicate_message_and_reused_id(self):
        self.store.begin_turn('c', 'm1', 'hello', [])
        with self.assertRaises(Conflict):
            self.store.begin_turn('c', 'm1', 'hello', [])
        self.store.finish_turn('c', 'm1', {'answer': 'Hi'})
        self.assertEqual(self.store.begin_turn('c', 'm1', 'hello', []), {'answer': 'Hi'})
        with self.assertRaises(ValueError):
            self.store.begin_turn('c', 'm1', 'different', [])

    def test_invalid_quantity_and_unknown_fields_rejected(self):
        for change in [op('qty', True), op('qty', '128'), op('qty', -1), op('api_key', 'x')]:
            with self.assertRaises(ValueError):
                apply_patch(blank(), [change], self.sources)

    def test_storage_representation_is_canonical(self):
        a = apply_patch(blank(), [op('storage', '256 GB')], self.sources)
        self.assertEqual(value(a['items']['item_one'], 'storage'), '256GB')

    def test_grade_and_color_exclusions_are_independent(self):
        a = apply_patch(blank(), [op('grade', 'A+'), op('color_exclusions', 'white'),
                                op('color_flexible', True)], self.sources)
        self.assertEqual(value(a['items']['item_one'], 'color_exclusions'), 'white')
        self.assertTrue(value(a['items']['item_one'], 'color_flexible'))

    def test_stale_reply_and_undeclared_question_blocked(self):
        for args in [dict(revision=1, action='answer', targets=[], field='', answer='Noted.'),
                     dict(revision=0, action='answer', targets=[], field='', answer='Which grade?')]:
            with self.assertRaises((ValueError, Conflict)):
                validate_reply(blank(), args, '')

    def test_unverified_numeric_price_blocked(self):
        with self.assertRaises(ValueError):
            validate_reply(blank(), dict(revision=0, action='answer', targets=[], field='', answer='The rate is $256.'), '')

    def test_cancel_keeps_history(self):
        a = apply_patch(blank(), [op('model', 'Device Alpha'), op('model', 'Device Beta', 'item_two')], self.sources)
        a = apply_patch(a, [dict(operation='cancel', target='item_one', source_id='m1', quote='customer evidence')], self.sources)
        self.assertEqual(set(a['items']), {'item_two'})
        self.assertIn('item_one', a['cancelled'])

    def test_shared_requirement_survives_before_items_and_restart(self):
        a = apply_patch(blank(), [op('storage','128GB','defaults'),op('qty',10,'defaults')], self.sources)
        self.store.save('c',a,0)
        a = Store(self.store.path).load('c')
        a = apply_patch(a,[op('model','Device Alpha')],self.sources)
        self.assertEqual(value(a['items']['item_one'],'storage'),'128GB')
        self.assertEqual(value(a['items']['item_one'],'qty'),10)
        self.assertEqual(a['items']['item_one']['storage']['inherited_from'],'defaults')

    def test_line_override_wins_and_new_items_still_inherit_default(self):
        a = apply_patch(blank(),[op('model','Device Alpha'),op('storage','512GB'),
                                op('storage','128GB','defaults')],self.sources)
        self.assertEqual(value(a['items']['item_one'],'storage'),'512GB')
        a = apply_patch(a,[op('model','Device Beta','item_two')],self.sources)
        self.assertEqual(value(a['items']['item_two'],'storage'),'128GB')
        self.assertEqual(value(a['items']['item_one'],'storage'),'512GB')

    def test_shared_defaults_do_not_resurrect_cancelled_items(self):
        a = apply_patch(blank(),[op('model','Device Alpha'),op('storage','128GB','defaults')],self.sources)
        a = apply_patch(a,[dict(operation='cancel',target='item_one',source_id='m1',quote='customer evidence')],self.sources)
        a = apply_patch(a,[op('grade','A+','defaults')],self.sources)
        self.assertEqual(a['items'],{})

    def test_ambiguous_default_cannot_propagate_to_future_models(self):
        with self.assertRaises(ValueError):
            apply_patch(blank(),[op('qty',10,'defaults',status='ambiguous')],self.sources)

    def test_explicit_human_request_has_exit_without_complete_order(self):
        args=dict(revision=0,action='escalate',targets=[],field='',answer='Your request for a sales colleague is noted.',request_quote='I want a human')
        validate_reply(blank(),args,'I want a human')
        with self.assertRaises(ValueError):
            validate_reply(blank(),args,'What storage options are there?')


class FailingProvider:
    async def complete(self, messages, tools):
        raise TimeoutError('simulated provider outage')


class ScriptedProvider:
    def __init__(self):
        self.n = 0

    async def complete(self, messages, tools):
        self.n += 1
        if self.n == 1:
            name, args = 'update_profile', {'expected_revision': 0, 'operations': [op('grade', 'A+')],
                'reviewed_sources': [{'source_id': 'm1', 'reason': 'Saved grade.'}]}
        else:
            saved = json.loads(messages[-1]['content'])['profile']
            assert saved['items']['item_one']['grade']['value'] == 'A+'
            name, args = 'finish_turn', dict(revision=saved['revision'], action='ask',
                answer='A+ noted. Which model do you need?', question_id='ask:item_one:model')
        return {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_' + str(self.n), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_question_id_returns_current_options_for_repair(self):
        class WrongThenRepair(ScriptedProvider):
            async def complete(self, messages, tools):
                if self.n == 1:
                    self.n += 1
                    args = dict(revision=1, action='ask', answer='Which model?', question_id='ask:wrong:model')
                    return {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'bad', 'type': 'function', 'function': {'name': 'finish_turn', 'arguments': json.dumps(args)}}]}
                if self.n == 2:
                    saved = json.loads(messages[-1]['content'])['saved']
                    self.n += 1
                    question = next(q for q in saved['allowed_questions'] if q['field'] == 'model')
                    args = dict(revision=saved['profile']['revision'], action='ask', answer='Which model?', question_id=question['id'])
                    return {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'repaired', 'type': 'function', 'function': {'name': 'finish_turn', 'arguments': json.dumps(args)}}]}
                return await super().complete(messages, tools)
        with tempfile.TemporaryDirectory() as tmp:
            result = await ProfileAgent(Store(Path(tmp) / 'db'), WrongThenRepair()).get_reply(
                'customer evidence', [], 'c', {'_message_id': 'm1'})
            self.assertEqual(result['answer'], 'Which model?')
            self.assertEqual(sum(not t['ok'] for t in result['tool_trace']), 1)

    async def test_write_result_used_before_reply_and_idempotent_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            agent = ProfileAgent(Store(Path(tmp) / 'db'), provider)
            args = ('customer evidence', [], 'test', {'_message_id': 'm1'})
            first = await agent.get_reply(*args)
            self.assertTrue(first['answer'])
            self.assertEqual(first, await agent.get_reply(*args))
            self.assertEqual(provider.n, 2)

    async def test_provider_failure_keeps_inbound_and_existing_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db')
            store.save('c', apply_patch(blank(), [op('grade', 'A+')], {'m1': 'customer evidence'}), 0)
            agent = ProfileAgent(store, FailingProvider())
            result = await agent.get_reply('10 each', [], 'c', {'_message_id': 'm2'})
            self.assertEqual(result['answer'], '')
            self.assertEqual(result['send_plan']['split_messages'], [])
            self.assertEqual(value(store.load('c')['items']['item_one'], 'grade'), 'A+')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT message,status FROM turns WHERE mid="m2"').fetchone(), ('10 each', 'failed'))

    async def test_save_failure_never_claims_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db')
            def broken(*args):
                raise OSError('simulated disk failure')
            store.save = broken
            result = await ProfileAgent(store, ScriptedProvider()).get_reply('customer evidence', [], 'c', {'_message_id': 'm1'})
            self.assertEqual(result['answer'], '')
            self.assertEqual(store.load('c')['items'], {})


if __name__ == '__main__':
    unittest.main()
