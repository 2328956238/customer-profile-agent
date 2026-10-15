"""Fixed synthetic Agent evaluations; offline scripted checks are not LLM scores."""
import argparse
import asyncio
import copy
import hashlib
import json
import math
import re
import socket
import statistics
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from .agent import ProfileAgent
from .mock_provider import MockProvider
from .policy import requirements
from .profile_store import FIELDS, Store


class OfflineNetworkBlocked(RuntimeError):
    pass


@contextmanager
def offline_network():
    """Process-local guard for the standalone evaluator, not a server sandbox."""
    def blocked(*args, **kwargs):
        raise OfflineNetworkBlocked('network disabled during offline evaluation')
    with ExitStack() as stack:
        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket.socket, 'sendto'), (socket, 'create_connection'),
                            (socket, 'getaddrinfo')):
            stack.enter_context(patch.object(owner, name, blocked))
        yield


def _hash(dataset):
    raw = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError(name + ' must be a 1–100 character ASCII identifier')


def _validate_dataset(dataset):
    if not isinstance(dataset, dict) or dataset.get('synthetic') is not True:
        raise ValueError('evaluation requires an explicitly synthetic dataset')
    cases = dataset.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('dataset must have nonempty cases')
    case_ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('cases must be objects')
        _identifier(case.get('id'), 'case id')
        if case['id'] in case_ids or type(case.get('requires_live', False)) is not bool:
            raise ValueError('case ids must be unique and requires_live must be boolean')
        case_ids.add(case['id'])
        turns = case.get('turns')
        if not isinstance(turns, list) or not turns:
            raise ValueError('each case requires turns')
        message_ids = set()
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError('turns must be objects')
            _identifier(turn.get('message_id'), 'message id')
            if turn['message_id'] in message_ids:
                raise ValueError('message ids must be unique within a case')
            message_ids.add(turn['message_id'])
            if not isinstance(turn.get('message'), str) or not 1 <= len(turn['message']) <= 10000:
                raise ValueError('messages must contain 1–10000 characters')
            expected = turn.get('expect')
            keys = {'actions', 'facts', 'absent_facts', 'question_fields', 'ready', 'forbid_handoff', 'refusal_terms'}
            if not isinstance(expected, dict) or set(expected) - keys:
                raise ValueError('unknown or missing expectation fields')
            if (not isinstance(expected.get('actions'), list) or not expected['actions']
                    or not all(value in ('ask', 'clarify', 'answer', 'handoff', 'escalate') for value in expected['actions'])):
                raise ValueError('expect.actions must contain supported actions')
            for name in ('facts', 'absent_facts'):
                labels = expected.get(name, [])
                if not isinstance(labels, list):
                    raise ValueError(name + ' must be a list')
                for label in labels:
                    if not isinstance(label, dict) or not isinstance(label.get('target'), str) or not isinstance(label.get('field'), str):
                        raise ValueError('fact labels require target and field')
                    if name == 'facts' and ('value' not in label or label.get('status', 'confirmed') not in ('confirmed', 'ambiguous')):
                        raise ValueError('facts require a value and valid status')
                    if label['target'] not in ('customer', 'order', 'defaults', 'all_items') and not label['target'].startswith('item:'):
                        raise ValueError('unsupported fact target')
                    kind = label['target'] if label['target'] in ('customer', 'order', 'defaults') else 'item'
                    if label['field'] not in FIELDS[kind]:
                        raise ValueError('unknown labelled field')
            if 'question_fields' in expected and (not isinstance(expected['question_fields'], list) or not expected['question_fields']
                    or not all(value is None or isinstance(value, str) for value in expected['question_fields'])):
                raise ValueError('question_fields must be a nonempty list of field names or null')
            for name in ('ready', 'forbid_handoff'):
                if name in expected and type(expected[name]) is not bool:
                    raise ValueError(name + ' must be boolean')
            if 'refusal_terms' in expected and (not isinstance(expected['refusal_terms'], list) or not expected['refusal_terms']
                    or not all(isinstance(value, str) and value.strip() for value in expected['refusal_terms'])):
                raise ValueError('refusal_terms must contain nonempty literal phrases')
    return cases


def _targets(profile, target):
    if target in ('customer', 'order', 'defaults'):
        return [profile.get(target, {})]
    items = profile.get('items', {})
    if target == 'all_items':
        return list(items.values())
    name = ' '.join(target.removeprefix('item:').casefold().split())
    return [facts for facts in items.values()
            if ' '.join(str(facts.get('model', {}).get('value', '')).casefold().split()) == name]


def _fact_match(profile, expected):
    targets = _targets(profile, expected['target'])
    if len(targets) != 1:
        return False
    fact = targets[0].get(expected['field'], {})
    actual, wanted = fact.get('value'), expected['value']
    return (type(actual) is type(wanted) and actual == wanted
            and fact.get('status') == expected.get('status', 'confirmed'))


def _evidence_checks(profile, sources):
    values = list(profile.get('items', {}).values()) + list(profile.get('cancelled', {}).values())
    values.extend(profile.get(target, {}) for target in ('customer', 'order', 'defaults'))
    results = []
    for facts in values:
        for fact in facts.values():
            if not isinstance(fact, dict):
                results.append(False)
                continue
            quote, source = fact.get('quote'), fact.get('source_id')
            results.append(isinstance(quote, str) and bool(quote.strip()) and isinstance(source, str)
                           and source in sources and quote in sources[source])
    return results


def _checks(result, profile, expected, sources):
    checks = {'fields': [], 'actions': [], 'questions': [], 'evidence': [], 'safety': [], 'refusal': []}
    checks['fields'].extend(_fact_match(profile, label) for label in expected.get('facts', []))
    for label in expected.get('absent_facts', []):
        checks['fields'].append(all(label['field'] not in target for target in _targets(profile, label['target'])))
    checks['actions'].append(result.get('action') in expected['actions'])
    if 'question_fields' in expected:
        checks['questions'].append((profile.get('pending_question') or {}).get('field') in expected['question_fields'])
    ready = requirements(profile)['ready']
    if 'ready' in expected:
        checks['safety'].append(ready is expected['ready'])
    if expected.get('forbid_handoff'):
        checks['safety'].append(result.get('action') != 'handoff')
    checks['safety'].append(result.get('action') != 'handoff' or ready)
    checks['safety'].append(result.get('external_delivery') is False)
    checks['safety'].append(not bool(re.search(r'[$€£]\s*\d|\d+(?:\.\d+)?\s*(?:USD|EUR|GBP)\b',
                                               str(result.get('answer', '')), re.I)))
    checks['evidence'] = _evidence_checks(profile, sources)
    if 'refusal_terms' in expected:
        answer = str(result.get('answer', '')).casefold()
        checks['refusal'].append(any(term.casefold() in answer for term in expected['refusal_terms']))
    return checks


def _safe_model(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}', value)
            or '://' in value or value.lower().startswith(('sk-', 'bearer', 'token'))):
        return None
    return value


async def run_evaluation(provider, dataset, *, mode='mock', model=None, timeout=60, max_steps=10):
    """Run synthetic conversations in isolated stores; never serialize raw answers.

    `mode='mock'` blocks sockets and skips requires_live cases. `mode='live'` is
    an explicit caller opt-in; this function never loads credentials by itself.
    The injected provider remains owned by its caller, including aclose().
    """
    dataset = copy.deepcopy(dataset)
    cases = _validate_dataset(dataset)
    if mode not in ('mock', 'live'):
        raise ValueError('mode must be mock or live')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    if type(max_steps) is not int or not 1 <= max_steps <= 100:
        raise ValueError('max_steps must be 1–100')
    aggregate = {key: [] for key in ('fields', 'actions', 'questions', 'evidence', 'safety', 'refusal')}
    case_results, failures, timings = [], [], []
    executed_cases = passed_cases = failed_cases = skipped_cases = executed_turns = skipped_turns = failed_turns = 0
    with ExitStack() as stack:
        if mode == 'mock':
            stack.enter_context(offline_network())
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='profile-evals-'))
        for index, case in enumerate(cases):
            if case.get('requires_live') and mode != 'live':
                skipped_cases += 1
                skipped_turns += len(case['turns'])
                case_results.append({'id': case['id'], 'status': 'skipped', 'reason': 'requires_explicit_live_mode',
                                     'turns_total': len(case['turns']), 'turns': []})
                continue
            executed_cases += 1
            store = Store(Path(directory) / f'case-{index}.sqlite3')
            agent = ProfileAgent(store, provider, timeout=timeout, max_steps=max_steps)
            sources, turn_results = {}, []
            for turn in case['turns']:
                executed_turns += 1
                sources[turn['message_id']] = turn['message']
                start = time.perf_counter()
                error = None
                try:
                    result = await agent.get_reply(turn['message'], [], 'synthetic-customer',
                                                   {'_message_id': turn['message_id']})
                except Exception:
                    result = {}
                    error = 'agent_exception'
                elapsed = round((time.perf_counter() - start) * 1000, 3)
                timings.append(elapsed)
                runtime_failed = error is not None or not isinstance(result, dict) or bool(result.get('retryable'))
                if not isinstance(result, dict):
                    result = {}
                checks = _checks(result, store.load('synthetic-customer'), turn['expect'], sources)
                failed = ['runtime_failed'] if runtime_failed else []
                for category, values in checks.items():
                    aggregate[category].extend(values)
                    failed.extend(f'{category}:{i}' for i, passed in enumerate(values) if not passed)
                if failed:
                    failed_turns += 1
                    failures.append({'case_id': case['id'], 'message_id': turn['message_id'], 'checks': failed})
                turn_results.append({'message_id': turn['message_id'], 'status': 'failed' if failed else 'passed',
                                     'latency_ms': elapsed, 'failed_checks': failed,
                                     'counts': {name: {'passed': sum(values), 'total': len(values)} for name, values in checks.items()}})
            case_failed = any(turn['status'] == 'failed' for turn in turn_results)
            failed_cases += case_failed
            passed_cases += not case_failed
            case_results.append({'id': case['id'], 'status': 'failed' if case_failed else 'passed',
                                 'turns_total': len(case['turns']), 'turns': turn_results})
    metrics = {name: {'passed': sum(values), 'total': len(values),
                      'rate': round(sum(values) / len(values), 6) if values else None}
               for name, values in aggregate.items()}
    ordered = sorted(timings)
    percentile = lambda p: ordered[max(0, math.ceil(p * len(ordered)) - 1)] if ordered else None
    return {'dataset_sha256': _hash(dataset), 'synthetic': True, 'mode': mode,
            'provider': type(provider).__name__, 'model': _safe_model(model),
            'scripted_provider': isinstance(provider, MockProvider),
            'network_allowed': mode == 'live',
            'interpretation': 'scripted offline engineering checks; not real LLM accuracy' if mode == 'mock'
                              else 'synthetic labelled checks of the supplied provider; not production accuracy',
            'counts': {'cases_total': len(cases), 'cases_executed': executed_cases, 'cases_passed': passed_cases,
                       'cases_failed': failed_cases, 'cases_skipped': skipped_cases,
                       'turns_total': sum(len(case['turns']) for case in cases), 'turns_executed': executed_turns,
                       'turns_failed': failed_turns, 'turns_skipped': skipped_turns},
            'metrics': metrics,
            'latency_ms': {'samples': len(timings), 'min': min(timings) if timings else None,
                           'median': round(statistics.median(timings), 3) if timings else None,
                           'p95': percentile(.95), 'max': max(timings) if timings else None},
            'failure_samples': failures, 'cases': case_results}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Evaluate synthetic Agent fixtures; offline by default')
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--live', action='store_true', help='Explicitly allow configured provider calls on synthetic fixtures')
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding='utf-8'))
    _validate_dataset(dataset)  # Reject invalid fixtures before reading live configuration.

    async def run():
        if args.live:
            from .provider import Provider
            provider = Provider.configured()
        else:
            provider = MockProvider()
        try:
            return await run_evaluation(provider, dataset, mode='live' if args.live else 'mock',
                                        model=getattr(provider, 'model', None))
        finally:
            if args.live:
                await provider.aclose()

    try:
        report = asyncio.run(run())
    except Exception as exc:
        # No exception message, provider URL, key, prompt or output is printed.
        parser.exit(2, 'Evaluation failed: ' + type(exc).__name__ + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['counts']['cases_failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
