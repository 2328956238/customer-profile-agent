"""Local, content-free request traces; usage is reported, never guessed."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import json
import sqlite3
import time
import uuid


current_trace = ContextVar('support_trace', default=None)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Trace:
    operation: str
    max_tokens: int = 12000
    max_attempts: int = 20
    input_rate: Decimal | None = None
    output_rate: Decimal | None = None
    pricing_revision: str = 'unconfigured'
    max_cost: Decimal | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    spans: list = field(default_factory=list)

    def __post_init__(self):
        for n in (self.max_tokens, self.max_attempts):
            if type(n) is not int or n <= 0:
                raise ValueError('trace budgets must be positive integers')
        for rate in (self.input_rate, self.output_rate, self.max_cost):
            if rate is not None and (not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0):
                raise ValueError('prices and cost budget require finite nonnegative Decimal values')

    def check_budget(self):
        model = [s for s in self.spans if s['kind'] == 'model']
        if len(model) >= self.max_attempts:
            raise BudgetExceeded('model attempt budget exceeded')
        if sum(s.get('input_tokens', 0) + s.get('output_tokens', 0) for s in model) >= self.max_tokens:
            raise BudgetExceeded('reported token budget exceeded')
        cost = self.summary()['known_cost_lower_bound_usd']
        if self.max_cost is not None and cost is not None and Decimal(cost) >= self.max_cost:
            raise BudgetExceeded('estimated cost budget exceeded')

    def check_spent(self):
        report = self.summary()
        if sum(s.get('input_tokens', 0) + s.get('output_tokens', 0) for s in self.spans) > self.max_tokens:
            raise BudgetExceeded('reported token budget exceeded')
        cost = report['known_cost_lower_bound_usd']
        if self.max_cost is not None and cost is not None and Decimal(cost) > self.max_cost:
            raise BudgetExceeded('estimated cost budget exceeded')

    def model(self, *, model, elapsed_ms, usage=None, error=None):
        span = {'span_id': uuid.uuid4().hex, 'parent_id': self.trace_id, 'kind': 'model',
                'model': str(model)[:100], 'elapsed_ms': round(elapsed_ms, 3), 'error': error}
        if (isinstance(usage, dict) and type(usage.get('prompt_tokens')) is int
                and type(usage.get('completion_tokens')) is int
                and usage['prompt_tokens'] >= 0 and usage['completion_tokens'] >= 0):
            span.update(input_tokens=usage['prompt_tokens'], output_tokens=usage['completion_tokens'])
        self.spans.append(span)

    def tool(self, name, elapsed_ms, ok):
        if name not in ('read_profile', 'update_profile', 'finish_turn'):
            name = 'unknown_tool'
        self.spans.append({'span_id': uuid.uuid4().hex, 'parent_id': self.trace_id,
                           'kind': 'tool', 'tool': name, 'elapsed_ms': round(elapsed_ms, 3), 'ok': bool(ok)})

    def summary(self):
        model = [s for s in self.spans if s['kind'] == 'model']
        complete = all('input_tokens' in s for s in model)
        it = sum(s.get('input_tokens', 0) for s in model)
        ot = sum(s.get('output_tokens', 0) for s in model)
        priced = self.input_rate is not None and self.output_rate is not None
        lower = ((Decimal(it) * self.input_rate + Decimal(ot) * self.output_rate) / Decimal(1000000)
                 if priced else None)
        cost = lower if complete else None
        return {'model_attempts': len(model), 'usage_complete': complete,
                'input_tokens': it if complete else None, 'output_tokens': ot if complete else None,
                'estimated_cost_usd': str(cost) if cost is not None else ('0' if not model else None),
                'known_cost_lower_bound_usd': str(lower) if lower is not None else None,
                'pricing_revision': self.pricing_revision,
                'cost_basis': 'operator-supplied USD per million tokens; not a provider invoice',
                'billing_unknown_attempts': sum('input_tokens' not in s for s in model),
                'spans': self.spans}


class TraceStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS support_traces (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, actor TEXT NOT NULL,
              trace_id TEXT NOT NULL UNIQUE, operation TEXT NOT NULL, status INTEGER NOT NULL,
              elapsed_ms REAL NOT NULL, created REAL NOT NULL, body TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS support_traces_tenant_seq ON support_traces(tenant, seq DESC);''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, trace, principal, status, elapsed_ms):
        with self.connect() as db:
            db.execute('INSERT INTO support_traces(tenant,actor,trace_id,operation,status,elapsed_ms,created,body) '
                       'VALUES(?,?,?,?,?,?,?,?)', (principal.tenant_id, principal.actor_id, trace.trace_id,
                        trace.operation, status, elapsed_ms, time.time(), json.dumps(trace.summary())))

    def page(self, tenant_id, limit=50, before_id=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        if before_id is not None and (type(before_id) is not int or not 0 < before_id <= 9223372036854775807):
            raise ValueError('before_id must be a positive integer')
        with self.connect() as db:
            rows = db.execute('SELECT seq,trace_id,operation,status,elapsed_ms,created,body FROM support_traces '
                              'WHERE tenant=? AND seq<? ORDER BY seq DESC LIMIT ?',
                              (tenant_id, before_id or 9223372036854775807, limit + 1)).fetchall()
        return {'items': [dict(zip(('id','trace_id','operation','status','elapsed_ms','created','usage'),
                            (*r[:6], json.loads(r[6])))) for r in rows[:limit]],
                'next_before_id': rows[limit-1][0] if len(rows) > limit else None}
