"""Tenant-scoped, versioned lexical retrieval and strictly extractive answers.

This is an offline portfolio module, not a semantic embedding service. Knowledge
references have their own namespace and never become customer-message evidence.
"""
import asyncio
from .telemetry import observe_retrieval
import copy
import hashlib
import inspect
import json
import math
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path


class KnowledgeConflict(ValueError):
    """A document version is immutable or its expected current version changed."""


_ANY = object()
METHODS = ('bm25', 'tfidf', 'hybrid')
_STOPWORDS = frozenset('a an the is are was were be to of for in on and or with '
                      'what which how do does can could please tell me about'.split())


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _string(value, name, limit, *, allow_blank=False):
    if not isinstance(value, str) or len(value) > limit or (not allow_blank and not value.strip()):
        raise ValueError(f'{name} must be a nonempty string of at most {limit} characters')
    if '\x00' in value:
        raise ValueError(name + ' must not contain NUL')
    return value


def tokenize(text):
    """Unicode words plus CJK bigrams; no stemming or learned semantic model."""
    normalized = unicodedata.normalize('NFKC', text).casefold()
    out = []
    for word in re.findall(r'[\u3400-\u9fff]+|[^\W_]+', normalized):
        if re.fullmatch(r'[\u3400-\u9fff]+', word):
            out.extend(word[i:i + 2] for i in range(len(word) - 1)) if len(word) > 1 else out.append(word)
        elif word not in _STOPWORDS:
            out.append(word)
    return out


def _split(text, size, overlap):
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind('\n', start + size // 2, end),
                           text.rfind(' ', start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def _rank(rows, query_terms, method):
    """All rows are already tenant-filtered, including IDF corpus statistics."""
    counts = [Counter(tokenize(row['title'] + '\n' + row['text'])) for row in rows]
    df = Counter(term for terms in counts for term in terms)
    lengths = [sum(terms.values()) for terms in counts]
    mean_length = sum(lengths) / len(rows) if rows else 0
    query = Counter(query_terms)
    n = len(rows)
    bm25, tfidf = [], []
    query_weights = {term: (1 + math.log(freq)) * (math.log((n + 1) / (df[term] + 1)) + 1)
                     for term, freq in query.items()}
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values()))
    for terms, length in zip(counts, lengths):
        bm_score = 0.0
        for term in query:
            frequency = terms[term]
            if frequency:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                bm_score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / mean_length))
        bm25.append(bm_score)
        weights = {term: (1 + math.log(freq)) * (math.log((n + 1) / (df[term] + 1)) + 1)
                   for term, freq in terms.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values()))
        tfidf.append(sum(query_weights[term] * weights.get(term, 0) for term in query) /
                     (query_norm * norm) if query_norm and norm else 0.0)
    if method != 'hybrid':
        return bm25 if method == 'bm25' else tfidf
    # Reciprocal rank fusion of two lexical retrievers, not semantic embeddings.
    scores = [0.0] * n
    for values in (bm25, tfidf):
        ranked = sorted((i for i, score in enumerate(values) if score > 0),
                        key=lambda i: (-values[i], rows[i]['citation_id']))
        for rank, index in enumerate(ranked, 1):
            scores[index] += 1 / (60 + rank)
    return scores


class KnowledgeBase:
    def __init__(self, path, *, chunk_size=600, overlap=80):
        if type(chunk_size) is not int or not 64 <= chunk_size <= 2000:
            raise ValueError('chunk_size must be an integer between 64 and 2000')
        if type(overlap) is not int or not 0 <= overlap < chunk_size // 2:
            raise ValueError('overlap must be a nonnegative integer below half the chunk size')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.chunk_size, self.overlap = chunk_size, overlap
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS knowledge_documents (
              tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT NOT NULL,
              title TEXT NOT NULL, content_sha256 TEXT NOT NULL, chunk_count INTEGER NOT NULL,
              PRIMARY KEY(tenant_id, doc_id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_versions (
              tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT NOT NULL,
              content_sha256 TEXT NOT NULL, PRIMARY KEY(tenant_id, doc_id, version)
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
              tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT NOT NULL,
              citation_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
              start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, text TEXT NOT NULL,
              PRIMARY KEY(tenant_id, citation_id),
              FOREIGN KEY(tenant_id, doc_id) REFERENCES knowledge_documents(tenant_id, doc_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS knowledge_chunks_document
              ON knowledge_chunks(tenant_id, doc_id, chunk_index);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def upsert(self, tenant_id, doc_id, version, title, text, *, expected_version=_ANY):
        """Atomically replace a document and chunks; an old version cannot revive.

        Versions are opaque immutable labels, not sortable timestamps. Pass
        expected_version (None for creation) for compare-and-swap publication.
        """
        _string(tenant_id, 'tenant_id', 100)
        _string(doc_id, 'doc_id', 200)
        _string(version, 'version', 100)
        _string(title, 'title', 300)
        _string(text, 'text', 100000)
        if expected_version is not _ANY and expected_version is not None:
            _string(expected_version, 'expected_version', 100)
        content_hash = _digest({'title': title, 'text': text})
        chunks = list(_split(text, self.chunk_size, self.overlap))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM knowledge_documents WHERE tenant_id=? AND doc_id=?',
                             (tenant_id, doc_id)).fetchone()
            if expected_version is not _ANY and (old['version'] if old else None) != expected_version:
                raise KnowledgeConflict('current document version changed')
            if old and old['version'] == version:
                if old['content_sha256'] != content_hash:
                    raise KnowledgeConflict('a document version is immutable; publish a new version')
                return {key: old[key] for key in ('doc_id', 'version', 'title', 'content_sha256', 'chunk_count')}
            if db.execute('SELECT 1 FROM knowledge_versions WHERE tenant_id=? AND doc_id=? AND version=?',
                          (tenant_id, doc_id, version)).fetchone():
                raise KnowledgeConflict('superseded or deleted document versions cannot be reused')
            db.execute('DELETE FROM knowledge_documents WHERE tenant_id=? AND doc_id=?', (tenant_id, doc_id))
            db.execute('INSERT INTO knowledge_documents VALUES(?,?,?,?,?,?)',
                       (tenant_id, doc_id, version, title, content_hash, len(chunks)))
            db.execute('INSERT INTO knowledge_versions VALUES(?,?,?,?)', (tenant_id, doc_id, version, content_hash))
            for index, (start, end, body) in enumerate(chunks):
                citation_id = 'kb:' + _digest(['knowledge.v1', tenant_id, doc_id, version, index, start, end, body])
                db.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?,?)',
                           (tenant_id, doc_id, version, citation_id, index, start, end, body))
        return {'doc_id': doc_id, 'version': version, 'title': title,
                'content_sha256': content_hash, 'chunk_count': len(chunks)}

    def delete(self, tenant_id, doc_id, *, expected_version=_ANY):
        _string(tenant_id, 'tenant_id', 100)
        _string(doc_id, 'doc_id', 200)
        if expected_version is not _ANY and expected_version is not None:
            _string(expected_version, 'expected_version', 100)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT version FROM knowledge_documents WHERE tenant_id=? AND doc_id=?',
                             (tenant_id, doc_id)).fetchone()
            if expected_version is not _ANY and (old['version'] if old else None) != expected_version:
                raise KnowledgeConflict('current document version changed')
            return db.execute('DELETE FROM knowledge_documents WHERE tenant_id=? AND doc_id=?',
                              (tenant_id, doc_id)).rowcount == 1

    @staticmethod
    def _evidence(row):
        return {'source_type': 'knowledge_document', **dict(row)}

    def _rows(self, db, tenant_id, citation_id=None):
        # Tenant filtering happens in SQL BEFORE ranking or corpus statistics.
        sql = ('SELECT c.citation_id,c.doc_id,c.version,d.title,c.chunk_index,c.start_offset,c.end_offset,c.text '
               'FROM knowledge_chunks c JOIN knowledge_documents d '
               'ON d.tenant_id=c.tenant_id AND d.doc_id=c.doc_id AND d.version=c.version '
               'WHERE c.tenant_id=?')
        params = [tenant_id]
        if citation_id is not None:
            sql += ' AND c.citation_id=?'
            params.append(citation_id)
        return db.execute(sql + ' ORDER BY c.doc_id,c.chunk_index', params).fetchall()

    def resolve_citation(self, tenant_id, citation_id):
        _string(tenant_id, 'tenant_id', 100)
        _string(citation_id, 'citation_id', 100)
        with self.connect() as db:
            rows = self._rows(db, tenant_id, citation_id)
        return self._evidence(rows[0]) if rows else None

    @observe_retrieval
    def search(self, tenant_id, query, top_k=3, *, method='bm25'):
        _string(tenant_id, 'tenant_id', 100)
        _string(query, 'query', 2000, allow_blank=True)
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError('top_k must be an integer between 1 and 20')
        if method not in METHODS:
            raise ValueError('method must be bm25, tfidf or hybrid')
        with self.connect() as db:
            rows = self._rows(db, tenant_id)
        terms = tokenize(query)
        scores = _rank(rows, terms, method) if rows and terms else [0.0] * len(rows)
        ranked = sorted((index for index, score in enumerate(scores) if score > 0),
                        key=lambda index: (-scores[index], rows[index]['citation_id']))[:top_k]
        hits = [{**self._evidence(rows[index]), 'score': round(scores[index], 8)} for index in ranked]
        return {'status': 'matched' if hits else 'abstain', 'method': method, 'hits': hits,
                'reason': None if hits else 'no_matching_evidence'}

    @staticmethod
    def _abstain(reason):
        return {'status': 'abstain', 'answer': '', 'citations': [], 'evidence': [], 'reason': reason}

    def _validate_answer(self, tenant_id, result, hits):
        """Exact excerpts only: valid citations alone cannot validate paraphrases."""
        if not isinstance(result, dict):
            return self._abstain('invalid_provider_result')
        answer, citations = result.get('answer'), result.get('citations')
        if (not isinstance(answer, str) or not answer.strip() or len(answer) > 10000
                or not isinstance(citations, list) or not citations or len(citations) > len(hits)
                or not all(isinstance(item, str) for item in citations)
                or len(set(citations)) != len(citations)):
            return self._abstain('invalid_provider_result')
        offered = {hit['citation_id']: hit for hit in hits}
        if any(citation not in offered for citation in citations):
            return self._abstain('citation_not_retrieved')
        current = []
        with self.connect() as db:
            db.execute('BEGIN')  # Validate every source against one current snapshot.
            for citation in citations:
                rows = self._rows(db, tenant_id, citation)
                if not rows:
                    return self._abstain('citation_no_longer_current')
                current.append(self._evidence(rows[0]))
        lines = [line.strip() for line in answer.splitlines() if line.strip()]
        if any(not any(line in source['text'] for source in current) for line in lines):
            return self._abstain('answer_not_supported_by_exact_excerpts')
        if any(not any(line in source['text'] for line in lines) for source in current):
            return self._abstain('unused_citation')
        return {'status': 'answered', 'answer': answer.strip(), 'citations': citations,
                'evidence': current, 'reason': None, 'answer_mode': 'extractive'}

    def extractive_answer(self, tenant_id, query, top_k=3, *, method='bm25'):
        result = self.search(tenant_id, query, top_k, method=method)
        if not result['hits']:
            return self._abstain('no_matching_evidence')
        first = result['hits'][0]
        return self._validate_answer(tenant_id, {'answer': first['text'].strip(),
                                                'citations': [first['citation_id']]}, result['hits'])

    async def answer(self, tenant_id, query, provider=None, top_k=3, *, method='bm25', provider_timeout=30):
        """A provider may select excerpts; it gets no storage, tenant key or tools.

        provider.answer(question, evidence) returns {answer: str, citations: list}.
        Sync and async providers are supported. No provider is created implicitly.
        """
        from .concurrency import run_blocking

        if provider is None:
            return await run_blocking(self.extractive_answer, tenant_id, query, top_k, method=method)
        if (isinstance(provider_timeout, bool) or not isinstance(provider_timeout, (int, float))
                or not math.isfinite(provider_timeout) or provider_timeout <= 0):
            raise ValueError('provider_timeout must be finite and positive')
        result = await run_blocking(self.search, tenant_id, query, top_k, method=method)
        if not result['hits']:
            return self._abstain('no_matching_evidence')
        try:
            # Keep caller-owned admission until blocking work really finishes;
            # a timeout cannot forcibly interrupt a synchronous provider's I/O.
            callback = provider.answer
            if inspect.iscoroutinefunction(callback):
                pending = callback(query, copy.deepcopy(result['hits']))
            else:
                pending = run_blocking(callback, query, copy.deepcopy(result['hits']))
            response = await asyncio.wait_for(pending, provider_timeout)
            if inspect.isawaitable(response):
                response = await asyncio.wait_for(response, provider_timeout)
        except Exception:
            return self._abstain('provider_unavailable')
        return await run_blocking(self._validate_answer, tenant_id, response, result['hits'])


def evaluate_dataset(dataset, *, top_k=3, method='bm25'):
    """Evaluate fixed synthetic labels, not model factuality or production quality.

    Input: {synthetic: true, documents: [{tenant_id,doc_id,version,title,text}],
            cases: [{id,tenant_id,query,expected_doc_ids: [...]}]}.
    Retrieval labels are never given to the ranker. Answer checks use the default
    exact-excerpt path and current-source validation; there is no LLM judge.
    """
    if not isinstance(dataset, dict) or dataset.get('synthetic') is not True:
        raise ValueError('this evaluator requires an explicitly synthetic dataset')
    documents, cases = dataset.get('documents'), dataset.get('cases')
    if not isinstance(documents, list) or not documents or not isinstance(cases, list) or not cases:
        raise ValueError('dataset requires nonempty documents and cases')
    dataset_hash = _digest(dataset)
    records, answerable, negative, recalled, relevant_total, hits, reciprocal, refusals = [], 0, 0, 0, 0, 0, 0.0, 0
    valid_citations = citation_count = answered_cases = relevant_citations = 0
    seen_cases, seen_documents = set(), set()
    with tempfile.TemporaryDirectory(prefix='knowledge-eval-') as folder:
        knowledge = KnowledgeBase(Path(folder) / 'knowledge.sqlite3')
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError('documents must be objects')
            key = (document.get('tenant_id'), document.get('doc_id'))
            if key in seen_documents:
                raise ValueError('evaluation documents must have unique tenant/document pairs')
            seen_documents.add(key)
            knowledge.upsert(**document)
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError('cases must be objects')
            case_id = _string(case.get('id'), 'case id', 100)
            expected = case.get('expected_doc_ids')
            if case_id in seen_cases or not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
                raise ValueError('cases need unique ids and expected_doc_ids lists')
            seen_cases.add(case_id)
            if len(set(expected)) != len(expected) or any((case.get('tenant_id'), doc) not in seen_documents for doc in expected):
                raise ValueError('expected documents must exist in the same tenant and be unique')
            result = knowledge.search(case['tenant_id'], case['query'], top_k, method=method)
            found = [hit['doc_id'] for hit in result['hits']]
            answer = knowledge.extractive_answer(case['tenant_id'], case['query'], top_k, method=method)
            if expected:
                answerable += 1
                relevant_total += len(expected)
                recalled += len(set(found) & set(expected))
                ranks = [rank for rank, doc in enumerate(found, 1) if doc in expected]
                hits += bool(ranks)
                reciprocal += 1 / min(ranks) if ranks else 0
            else:
                negative += 1
                refusals += answer['status'] == 'abstain'
            answered_cases += answer['status'] == 'answered'
            for citation in answer['citations']:
                citation_count += 1
                source = knowledge.resolve_citation(case['tenant_id'], citation)
                valid_citations += source is not None
                relevant_citations += source is not None and source['doc_id'] in expected
            records.append({'id': case_id, 'expected_doc_ids': expected, 'retrieved_doc_ids': found,
                            'retrieval_status': result['status'], 'answer_status': answer['status'],
                            'citation_count': len(answer['citations'])})
    ratio = lambda numerator, denominator: round(numerator / denominator, 6) if denominator else None
    return {'dataset_sha256': dataset_hash, 'synthetic': True, 'method': method, 'top_k': top_k,
            'scope': 'synthetic lexical retrieval and extractive citation checks; no LLM accuracy measurement',
            'counts': {'cases': len(cases), 'answerable_cases': answerable, 'no_evidence_cases': negative,
                       'relevant_documents': relevant_total, 'retrieved_relevant_documents': recalled,
                       'hit_cases': hits, 'correct_abstentions': refusals, 'answered_cases': answered_cases,
                       'citations': citation_count, 'valid_current_citations': valid_citations,
                       'label_relevant_citations': relevant_citations},
            'metrics': {'recall_at_k': ratio(recalled, relevant_total), 'hit_at_k': ratio(hits, answerable),
                        'mrr_at_k': ratio(reciprocal, answerable), 'abstention_accuracy': ratio(refusals, negative),
                        'current_citation_validity': ratio(valid_citations, citation_count),
                        'labelled_citation_precision': ratio(relevant_citations, citation_count)},
            'cases': records}


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Offline synthetic knowledge retrieval evaluation')
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--method', choices=METHODS, default='bm25')
    parser.add_argument('--top-k', type=int, default=3)
    args = parser.parse_args()
    report = evaluate_dataset(json.loads(args.dataset.read_text(encoding='utf-8')), top_k=args.top_k, method=args.method)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
