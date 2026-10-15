"""Meaningful denominator, refusal, evidence, isolation and no-network checks."""
import asyncio
import copy
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from customer_profile_agent.evals import main, run_evaluation
from customer_profile_agent.mock_provider import EXAMPLES, MockProvider
from customer_profile_agent.profile_store import blank


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/evals/synthetic-v1.json'


def dataset():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def test_offline_report_counts_skipped_complex_cases_without_fake_accuracy():
    report = asyncio.run(run_evaluation(MockProvider(), dataset()))
    assert report['counts'] == {'cases_total': 8, 'cases_executed': 1, 'cases_passed': 1,
                                'cases_failed': 0, 'cases_skipped': 7, 'turns_total': 11,
                                'turns_executed': 3, 'turns_failed': 0, 'turns_skipped': 8}
    assert report['metrics']['fields'] == {'passed': 12, 'total': 12, 'rate': 1}
    assert report['metrics']['actions']['total'] == 3
    assert report['metrics']['evidence'] == {'passed': 12, 'total': 12, 'rate': 1}
    assert report['metrics']['refusal'] == {'passed': 0, 'total': 0, 'rate': None}
    assert report['latency_ms']['samples'] == 3
    assert report['network_allowed'] is False
    assert 'not real LLM accuracy' in report['interpretation']
    assert all(row['status'] == 'skipped' for row in report['cases'][1:])
    raw = json.dumps(report, ensure_ascii=False)
    assert all(message not in raw for message in EXAMPLES)
    assert 'tool_trace' not in raw and 'quote' not in raw


def test_dataset_hash_changes_with_labels_and_wrong_labels_fail():
    original = dataset()
    changed = copy.deepcopy(original)
    changed['cases'][0]['turns'][2]['expect']['facts'][1]['value'] = 99
    first = asyncio.run(run_evaluation(MockProvider(), original))
    second = asyncio.run(run_evaluation(MockProvider(), changed))
    assert first['dataset_sha256'] != second['dataset_sha256']
    assert second['counts']['cases_failed'] == 1
    assert second['metrics']['fields']['rate'] < 1
    assert second['failure_samples'] == [{'case_id': 'scripted-demo', 'message_id': 'm3', 'checks': ['fields:1']}]


def test_network_attempt_is_blocked_and_secret_error_not_reported():
    class NetworkProvider:
        api_key = 'private-test-key-should-not-appear'

        async def complete(self, *_):
            socket.create_connection(('example.invalid', 443))

    with patch('socket.socket.connect') as underlying:
        report = asyncio.run(run_evaluation(NetworkProvider(), dataset()))
    underlying.assert_not_called()
    assert report['counts']['cases_failed'] == 1
    assert report['counts']['turns_failed'] == 3
    assert all('runtime_failed' in row['checks'] for row in report['failure_samples'])
    assert NetworkProvider.api_key not in json.dumps(report)
    assert 'example.invalid' not in json.dumps(report)


def test_provider_exception_does_not_leak_message_or_url():
    class FailedProvider:
        async def complete(self, *_):
            raise RuntimeError('secret-bearer-value and private customer message https://private.invalid')

    report = asyncio.run(run_evaluation(FailedProvider(), dataset(), model='sk-private-token'))
    assert report['model'] is None
    assert report['counts']['cases_failed'] == 1
    for secret in ('secret-bearer-value', 'private customer', 'private.invalid', 'sk-private-token'):
        assert secret not in json.dumps(report)


def test_explicit_live_mode_evaluates_complex_cases_and_exposes_mock_limitations():
    # This is an injected local fake, not a network or real-model run. Running the
    # skipped cases with the same script must fail rather than produce fake 100%.
    with patch('socket.socket.connect', side_effect=AssertionError('test never permits network')):
        report = asyncio.run(run_evaluation(MockProvider(), dataset(), mode='live', model='injected-test-script'))
    assert report['counts']['cases_executed'] == 8
    assert report['counts']['cases_skipped'] == 0
    assert report['counts']['cases_failed'] > 0
    assert report['metrics']['refusal']['total'] == 2
    assert report['metrics']['refusal']['passed'] == 0
    assert report['metrics']['fields']['rate'] < 1


def test_each_case_gets_isolated_store_with_same_message_ids():
    fixture = dataset()
    case = copy.deepcopy(fixture['cases'][0])
    case['id'] = 'another-identical-case'
    fixture['cases'] = [fixture['cases'][0], case]
    report = asyncio.run(run_evaluation(MockProvider(), fixture))
    assert report['counts']['cases_passed'] == 2
    assert report['counts']['turns_executed'] == 6
    assert report['metrics']['fields']['total'] == 24


def test_evidence_checks_persisted_state_instead_of_trusting_result_profile():
    fixture = dataset()
    fixture['cases'] = [fixture['cases'][0]]
    fixture['cases'][0]['turns'] = fixture['cases'][0]['turns'][:1]

    class CorruptStateAgent:
        def __init__(self, store, provider, **kwargs):
            self.store = store

        async def get_reply(self, message, history, customer, context):
            profile = blank()
            profile['items']['item_any'] = {
                'model': {'value': 'Device Alpha', 'status': 'confirmed', 'source_id': 'm1', 'quote': 'Device Alpha'},
                'storage': {'value': '128GB', 'status': 'confirmed', 'source_id': 'm1', 'quote': 'invented evidence'},
            }
            profile['pending_question'] = {'field': 'qty'}
            self.store.save(customer, profile, 0)
            return {'action': 'ask', 'answer': 'How many units?', 'external_delivery': False,
                    'profile': blank()}  # The evaluator must not use this detached result copy.

    with patch('customer_profile_agent.evals.ProfileAgent', CorruptStateAgent):
        report = asyncio.run(run_evaluation(MockProvider(), fixture))
    assert report['metrics']['fields']['rate'] == 1
    assert report['metrics']['evidence'] == {'passed': 1, 'total': 2, 'rate': 0.5}
    assert report['counts']['cases_failed'] == 1
    assert report['failure_samples'][0]['checks'] == ['evidence:1']


def test_default_cli_never_reads_live_configuration(capsys):
    with patch('customer_profile_agent.provider.Provider.configured', side_effect=AssertionError('must not load keys')):
        assert main([str(FIXTURE)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['mode'] == 'mock' and report['provider'] == 'MockProvider'


def test_explicit_live_cli_owns_and_closes_injected_provider(capsys):
    class FakeConfigured(MockProvider):
        model = 'local-test-only'
        closed = False

        async def aclose(self):
            self.closed = True

    fake = FakeConfigured()
    with patch('customer_profile_agent.provider.Provider.configured', return_value=fake) as configured:
        with patch('socket.socket.connect', side_effect=AssertionError('no real live calls in tests')):
            assert main([str(FIXTURE), '--live']) == 1
    configured.assert_called_once()
    assert fake.closed
    report = json.loads(capsys.readouterr().out)
    assert report['model'] == 'local-test-only'
    assert report['counts']['cases_skipped'] == 0


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(synthetic=False),
    lambda value: value['cases'].append(copy.deepcopy(value['cases'][0])),
    lambda value: value['cases'][0]['turns'][0]['expect'].update(ignored_label=True),
    lambda value: value['cases'][0]['turns'][0].update(message_id='unsafe/id'),
    lambda value: value['cases'][0]['turns'][0]['expect']['absent_facts'][0].update(field='unknown_field'),
])
def test_bad_fixtures_fail_before_provider_use(mutate):
    fixture = dataset()
    mutate(fixture)

    class MustNotRun:
        async def complete(self, *_):
            pytest.fail('invalid data must not call a provider')

    with pytest.raises(ValueError):
        asyncio.run(run_evaluation(MustNotRun(), fixture))
