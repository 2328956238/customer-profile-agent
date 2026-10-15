"""Offline knowledge lifecycle, isolation, evidence and fixed-label evaluation."""
import asyncio
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from customer_profile_agent.knowledge import KnowledgeBase, KnowledgeConflict, evaluate_dataset
from customer_profile_agent.profile_store import Store


@pytest.fixture
def kb(tmp_path):
    return KnowledgeBase(tmp_path / 'state.sqlite3')


def put(kb, doc='shipping', text='Standard shipping takes four working days.', *, tenant='a', version='v1', **options):
    return kb.upsert(tenant, doc, version, 'Fictional ' + doc, text, **options)


@pytest.mark.parametrize('method', ['bm25', 'tfidf', 'hybrid'])
def test_tenant_filter_precedes_ranking_and_idf(kb, method):
    put(kb)
    put(kb, 'refund', 'Refunds require a receipt.')
    before = kb.search('a', 'shipping', method=method)
    # Foreign-tenant documents must not alter scores or corpus statistics.
    for index in range(6):
        put(kb, str(index), 'shipping shipping shipping confidential-foreign', tenant='b')
    assert kb.search('a', 'shipping', method=method) == before
    assert kb.search('a', 'confidential-foreign', method=method)['status'] == 'abstain'
    assert kb.search('b', 'confidential-foreign', method=method)['hits']
    citation = before['hits'][0]['citation_id']
    assert kb.resolve_citation('b', citation) is None
    assert kb.resolve_citation('a', citation)['source_type'] == 'knowledge_document'


def test_same_document_version_is_idempotent_and_immutable(kb):
    first = put(kb)
    hit = kb.search('a', 'shipping')['hits'][0]
    assert put(kb) == first
    assert kb.search('a', 'shipping')['hits'][0] == hit
    with pytest.raises(KnowledgeConflict, match='immutable'):
        put(kb, text='Shipping changed without a version change.')
    assert kb.resolve_citation('a', hit['citation_id'])['text'] == hit['text']


def test_update_delete_and_restart_never_revive_old_citation(kb):
    put(kb)
    old = kb.search('a', 'shipping')['hits'][0]
    put(kb, version='v2', text='Shipping now takes six working days.', expected_version='v1')
    assert kb.resolve_citation('a', old['citation_id']) is None
    new = kb.search('a', 'shipping')['hits'][0]
    assert new['version'] == 'v2' and new['citation_id'] != old['citation_id']
    assert kb.delete('a', 'shipping', expected_version='v2') is True
    assert kb.delete('a', 'shipping') is False
    assert kb.search('a', 'shipping')['status'] == 'abstain'
    restarted = KnowledgeBase(kb.path)
    for version in ('v1', 'v2'):
        with pytest.raises(KnowledgeConflict, match='cannot be reused'):
            put(restarted, version=version)
    assert restarted.resolve_citation('a', new['citation_id']) is None
    put(restarted, version='v3')
    assert restarted.search('a', 'shipping')['hits'][0]['version'] == 'v3'


def test_conflicting_publish_does_not_overwrite_newer_version(kb):
    put(kb, expected_version=None)

    def publish(version):
        try:
            put(kb, version=version, expected_version='v1')
            return 'published'
        except KnowledgeConflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, ['v2', 'v3'])) == ['conflict', 'published']
    assert kb.search('a', 'shipping')['hits'][0]['version'] in ('v2', 'v3')
    with pytest.raises(KnowledgeConflict):
        kb.delete('a', 'shipping', expected_version='v1')


def test_chunk_replacement_rolls_back_atomically_on_failure(kb):
    put(kb)
    original = kb.search('a', 'shipping')
    with kb.connect() as db:
        db.execute("CREATE TRIGGER fail_new_chunks BEFORE INSERT ON knowledge_chunks "
                   "WHEN NEW.version='broken' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        put(kb, version='broken', text='Shipping new text.')
    assert kb.search('a', 'shipping') == original
    with kb.connect() as db:
        assert db.execute("SELECT count(*) FROM knowledge_versions WHERE version='broken'").fetchone()[0] == 0


def test_chunk_offsets_stability_and_shared_profile_database(tmp_path):
    path = tmp_path / 'shared.sqlite3'
    profile_store = Store(path)
    profile_store.begin_turn('customer', 'm1', 'hello', [])
    kb = KnowledgeBase(path, chunk_size=80, overlap=15)
    text = 'Shipping policy. ' * 20 + 'Warranty covers defects.'
    result = put(kb, text=text)
    assert result['chunk_count'] > 1
    hits = kb.search('a', 'shipping warranty', top_k=20)['hits']
    assert len({hit['citation_id'] for hit in hits}) == len(hits)
    for hit in hits:
        assert hit['text'] == text[hit['start_offset']:hit['end_offset']]
    assert kb.search('a', 'shipping warranty', top_k=20) == KnowledgeBase(path).search('a', 'shipping warranty', top_k=20)
    profile_store.finish_turn('customer', 'm1', {'answer': 'Hello'})
    assert profile_store.history('customer')[0]['content'] == 'hello'


@pytest.mark.parametrize('method', ['bm25', 'tfidf', 'hybrid'])
def test_relevant_topic_ranks_first_with_cjk_queries(kb, method):
    put(kb, 'warranty', '保修期限十二个月。Warranty covers manufacturing defects for twelve months.')
    put(kb, 'shipping', '标准配送四天。Standard shipping takes four working days.')
    put(kb, 'general', 'General information about products and policy.')
    assert kb.search('a', 'manufacturing warranty defects', method=method)['hits'][0]['doc_id'] == 'warranty'
    assert kb.search('a', '保修期限', method=method)['hits'][0]['doc_id'] == 'warranty'
    assert kb.search('a', ' ', method=method)['status'] == 'abstain'
    assert kb.search('a', 'cryptocurrency', method=method)['status'] == 'abstain'


def test_default_answer_is_current_evidence_and_not_customer_source(kb):
    put(kb)
    result = asyncio.run(kb.answer('a', 'shipping'))
    assert result['status'] == 'answered'
    assert result['answer'] == 'Standard shipping takes four working days.'
    assert result['citations'][0].startswith('kb:')
    assert result['evidence'][0]['source_type'] == 'knowledge_document'
    assert 'source_id' not in result['evidence'][0]


def test_no_match_abstains_without_invoking_provider(kb):
    put(kb)

    class MustNotCall:
        async def answer(self, *_):
            pytest.fail('provider should not be called without evidence')

    result = asyncio.run(kb.answer('a', 'nonexistent-unicorn', MustNotCall()))
    assert result == {'status': 'abstain', 'answer': '', 'citations': [], 'evidence': [], 'reason': 'no_matching_evidence'}


@pytest.mark.parametrize('async_provider', [True, False])
def test_injected_provider_can_select_exact_excerpt(kb, async_provider):
    put(kb)

    def select(question, evidence):
        assert question == 'shipping'
        return {'answer': 'four working days', 'citations': [evidence[0]['citation_id']]}

    class Select:
        if async_provider:
            async def answer(self, question, evidence):
                return select(question, evidence)
        else:
            def answer(self, question, evidence):
                return select(question, evidence)

    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        result = asyncio.run(kb.answer('a', 'shipping', Select()))
    assert result['status'] == 'answered' and result['answer'] == 'four working days'


@pytest.mark.parametrize('attack', ['invented', 'foreign', 'unretrieved', 'unsupported', 'mutation'])
def test_forged_irrelevant_or_unsupported_provider_answer_is_rejected(kb, attack):
    put(kb)
    put(kb, 'refund', 'Refunds take five days.')
    put(kb, 'foreign', 'Shipping secret foreign terms.', tenant='b')
    foreign = kb.search('b', 'shipping')['hits'][0]['citation_id']
    unrelated = kb.search('a', 'refund')['hits'][0]['citation_id']

    class Invalid:
        async def answer(self, question, evidence):
            citation = {'invented': 'kb:made-up', 'foreign': foreign,
                        'unretrieved': unrelated}.get(attack, evidence[0]['citation_id'])
            answer = 'Unlimited refunds are guaranteed.' if attack in ('unsupported', 'mutation') else evidence[0]['text']
            if attack == 'mutation':
                evidence[0]['text'] = answer
            return {'answer': answer, 'citations': [citation]}

    result = asyncio.run(kb.answer('a', 'shipping', Invalid(), top_k=1))
    assert result['status'] == 'abstain' and result['answer'] == ''
    assert result['reason'] in ('citation_not_retrieved', 'answer_not_supported_by_exact_excerpts')


@pytest.mark.parametrize('change', ['update', 'delete'])
def test_source_changed_during_generation_invalidates_answer(kb, change):
    put(kb)

    class Racing:
        async def answer(self, question, evidence):
            if change == 'update':
                put(kb, version='v2', text='Shipping takes eight days.')
            else:
                kb.delete('a', 'shipping')
            return {'answer': evidence[0]['text'], 'citations': [evidence[0]['citation_id']]}

    result = asyncio.run(kb.answer('a', 'shipping', Racing()))
    assert result['reason'] == 'citation_no_longer_current'
    assert result['answer'] == ''


def test_provider_failure_and_deadline_return_no_unvalidated_answer(kb):
    put(kb)

    class Failed:
        async def answer(self, *_):
            raise RuntimeError('should not leak provider details')

    class Slow:
        async def answer(self, *_):
            await asyncio.sleep(1)

    for provider in (Failed(), Slow()):
        result = asyncio.run(kb.answer('a', 'shipping', provider, provider_timeout=0.01))
        assert result['reason'] == 'provider_unavailable' and result['answer'] == ''
        assert 'details' not in json.dumps(result)


@pytest.mark.parametrize('args', [('', 'query', 3), ('a', 'query', True), ('a', 'query', 0),
                                  ('a', 'query', 21), ('a', 'x' * 2001, 3)])
def test_search_rejects_invalid_boundaries(kb, args):
    with pytest.raises(ValueError):
        kb.search(*args)


def test_fixed_synthetic_evaluation_reports_real_counts_and_hash():
    path = Path(__file__).resolve().parents[1] / 'fixtures/knowledge/synthetic-v1.json'
    dataset = json.loads(path.read_text())
    with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
        report = evaluate_dataset(dataset)
        repeated = evaluate_dataset(dataset)
    assert report == repeated
    assert report['counts']['cases'] == 14
    assert report['counts']['answerable_cases'] == 10
    assert report['counts']['no_evidence_cases'] == 4
    assert report['counts']['relevant_documents'] == 11
    assert report['metrics']['recall_at_k'] == 1
    assert report['metrics']['abstention_accuracy'] == 0.75  # Kept failure, not a fabricated perfect score.
    assert report['metrics']['current_citation_validity'] == 1
    assert report['metrics']['labelled_citation_precision'] == 0.909091
    assert len(report['dataset_sha256']) == 64
    modified = copy.deepcopy(dataset)
    modified['cases'][0]['query'] = 'unknown-new-topic'
    changed = evaluate_dataset(modified)
    assert changed['dataset_sha256'] != report['dataset_sha256']
    assert changed['metrics']['recall_at_k'] < report['metrics']['recall_at_k']


def test_evaluation_rejects_real_or_cross_tenant_labels():
    with pytest.raises(ValueError, match='synthetic'):
        evaluate_dataset({'synthetic': False})
    dataset = {'synthetic': True, 'documents': [dict(tenant_id='a', doc_id='d', version='v1', title='t', text='hello')],
               'cases': [dict(id='bad', tenant_id='b', query='hello', expected_doc_ids=['d'])]}
    with pytest.raises(ValueError, match='same tenant'):
        evaluate_dataset(dataset)
