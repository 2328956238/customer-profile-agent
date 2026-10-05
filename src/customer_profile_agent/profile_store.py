"""Revisioned, evidence-backed profiles derived from an existing agent core.

The public demo contains only generic fields and synthetic examples.
"""
import copy
import json
import re
import sqlite3
import time
from pathlib import Path

FIELDS = {
    "customer": {"name", "language"},
    "order": {"country", "freight", "quantity_mentioned", "quantity_total", "quantity_scope", "intent"},
    "defaults": {"storage", "grade", "qty"},
    "item": {"model", "storage", "grade", "qty", "color_preference", "color_flexible", "color_exclusions"},
}
REQUIRED = ("model", "qty")


class Conflict(Exception):
    pass


def blank():
    return {'revision': 0, 'customer': {}, 'order': {}, 'items': {}, 'defaults': {}, 'cancelled': {},
            'audit': [], 'pending_question': None, 'reviewed_revision': None}


def value(facts, field, default=None):
    fact = facts.get(field, {})
    return fact.get('value', default) if fact.get('status') == 'confirmed' else default


def fingerprint(profile):
    """Only requirement changes invalidate an already reviewed order."""
    def clean(facts):
        return {k: {'value': f['value'], 'status': f['status']} for k, f in facts.items()
                if k not in ('intent', 'purchase_stage', 'destination_raw')}
    return json.dumps({'items': {k: clean(v) for k, v in profile['items'].items()},
                       'order': clean(profile['order']), 'defaults': clean(profile.get('defaults', {}))}, sort_keys=True, ensure_ascii=False)


def apply_patch(profile, operations, sources):
    """All operations succeed or none do; exact user evidence is mandatory."""
    updated = copy.deepcopy(profile)
    updated.setdefault('defaults', {})
    changed_defaults, explicit_item_fields = set(), set()
    if not isinstance(operations, list) or len(operations) > 60:
        raise ValueError('operations must be a list of at most 60 changes')
    for op in operations:
        target, field = op['target'], op.get('field')
        source_id, quote = op['source_id'], op['quote']
        if not isinstance(quote, str) or not quote.strip() or quote not in sources.get(source_id, ''):
            raise ValueError('source must quote an actual customer message verbatim')
        if target in ('customer', 'order', 'defaults'):
            facts, kind = updated[target], target
        else:
            if not isinstance(target, str) or not target.startswith('item_') or len(target) > 80:
                raise ValueError('item target must be a stable item_ identifier')
            facts, kind = updated['items'].setdefault(target, {}), 'item'
        if op.get('operation', 'set') == 'cancel':
            if kind != 'item' or not facts:
                raise ValueError('can only cancel an existing item')
            updated['cancelled'][target] = updated['items'].pop(target)
        elif op.get('operation', 'set') == 'set':
            if field not in FIELDS[kind]:
                raise ValueError('unknown field')
            if 'status' not in op or 'value' not in op:
                raise ValueError('Each set operation MUST include status (confirmed or ambiguous) and value')
            status, val = op['status'], op['value']
            if status not in ('confirmed', 'ambiguous'):
                raise ValueError('status must be confirmed or ambiguous')
            if kind == 'defaults' and status != 'confirmed':
                raise ValueError('defaults require an explicit confirmed all-model requirement')
            if field in ('qty', 'quantity_total', 'quantity_mentioned'):
                if type(val) is not int or not 0 < val <= 1000000:
                    raise ValueError('quantity must be a positive integer')
            elif field == 'color_flexible':
                if type(val) is not bool:
                    raise ValueError('color_flexible must be boolean')
            elif not isinstance(val, str) or not val.strip() or len(val) > 1000:
                raise ValueError('field value must be a nonempty string')
            if field == 'storage' and isinstance(val, str):
                compact = ''.join(val.split()).upper()
                # French Go/To and English GB/TB denote the same capacity units.
                compact = re.sub(r'(?<=\d)GO$', 'GB', compact)
                compact = re.sub(r'(?<=\d)TO$', 'TB', compact)
                if re.fullmatch(r'\d+(?:\.\d+)?(?:GB|TB)', compact):
                    val = compact
            if field == 'freight' and status == 'confirmed' and val not in ('yes', 'no', 'self', 'assistance'):
                raise ValueError('invalid logistics value')
            old = copy.deepcopy(facts.get(field))
            facts[field] = {'value': val, 'status': status, 'source_id': source_id,
                            'quote': quote, 'revision': profile['revision'] + 1}
            if kind == 'defaults':
                changed_defaults.add(field)
            elif kind == 'item':
                explicit_item_fields.add((target, field))
            op = dict(op, old=old)
        else:
            raise ValueError('unknown operation')
        updated['audit'].append(dict(op, revision=profile['revision'] + 1))
    # Structured scope, not a language regex: a customer-declared shared default
    # has a durable location and supplies subsequently added items. Explicit line
    # changes in this patch win, regardless of operation order.
    for item_id, item in updated['items'].items():
        for field, fact in updated['defaults'].items():
            if (item_id, field) not in explicit_item_fields and (field in changed_defaults or field not in item):
                old = copy.deepcopy(item.get(field))
                item[field] = dict(copy.deepcopy(fact), inherited_from='defaults', revision=profile['revision'] + 1)
                updated['audit'].append({'operation': 'inherit', 'target': item_id, 'field': field,
                    'value': fact['value'], 'old': old, 'source_id': fact['source_id'],
                    'quote': fact['quote'], 'revision': profile['revision'] + 1})
    if fingerprint(updated) != fingerprint(profile):
        updated['reviewed_revision'] = None
        updated['reviewed_fingerprint'] = None
    return updated


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS profiles (cid TEXT PRIMARY KEY, revision INTEGER, body TEXT);
            CREATE TABLE IF NOT EXISTS turns (cid TEXT, mid TEXT, message TEXT, history TEXT,
              status TEXT, result TEXT, created REAL, PRIMARY KEY(cid,mid));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def load(self, cid):
        with self.connect() as db:
            row = db.execute('SELECT body FROM profiles WHERE cid=?', (cid,)).fetchone()
        return json.loads(row[0]) if row else blank()

    def save(self, cid, profile, expected):
        if type(expected) is not int or expected < 0:
            raise ValueError('expected revision must be a nonnegative integer')
        updated = copy.deepcopy(profile)
        updated['revision'] = expected + 1
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM profiles WHERE cid=?', (cid,)).fetchone()
            actual = row[0] if row else 0
            if actual != expected:
                raise Conflict('profile revision changed; reload and retry')
            db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?)',
                       (cid, expected + 1, json.dumps(updated, ensure_ascii=False)))
        return updated

    def begin_turn(self, cid, mid, message, history):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT message,status,result FROM turns WHERE cid=? AND mid=?', (cid, mid)).fetchone()
            if old:
                if old[0] != message:
                    raise ValueError('message id reused with different content')
                if old[1] == 'done':
                    return json.loads(old[2])
                if old[1] == 'running':
                    raise Conflict('this message is already being processed; recover explicitly after a crash')
                db.execute('UPDATE turns SET status=? WHERE cid=? AND mid=?', ('running', cid, mid))
            else:
                db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?,?)',
                           (cid, mid, message, json.dumps(history, ensure_ascii=False), 'running', None, time.time()))
        return None

    def finish_turn(self, cid, mid, result, status='done'):
        with self.connect() as db:
            db.execute('UPDATE turns SET status=?,result=? WHERE cid=? AND mid=?',
                       (status, json.dumps(result, ensure_ascii=False), cid, mid))

    def recover_turn(self, cid, mid):
        """Operator-only recovery after confirming the original worker has stopped."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute(
                'UPDATE turns SET status="failed" WHERE cid=? AND mid=? AND status="running"',
                (cid, mid))
            return cursor.rowcount == 1

    def history(self, cid):
        with self.connect() as db:
            rows = db.execute('SELECT mid,message,result FROM turns WHERE cid=? AND status="done" ORDER BY created', (cid,)).fetchall()
        out = []
        for mid, message, result in rows:
            out.extend([{'role': 'user', 'content': message, 'message_id': mid},
                        {'role': 'assistant', 'content': json.loads(result)['answer']}])
        return out
