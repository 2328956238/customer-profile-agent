"""2026-10-14 offline portfolio extension: at-least-once local delivery.

No network transport is implemented. SQLite claims fence acknowledgements; a
separate fictional receiver demonstrates stable-ID deduplication after a crash.
"""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def event_id_for(cid, mid):
    return hashlib.sha256(canonical(['handoff.v1', cid, mid]).encode()).hexdigest()


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be finite and positive')


def valid_time(now):
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('now must be finite')


def create_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS outbox (
      event_id TEXT PRIMARY KEY, cid TEXT NOT NULL, mid TEXT NOT NULL,
      payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
      lease_until REAL, claim_token TEXT, worker_id TEXT,
      created REAL NOT NULL, delivered_at REAL, last_error TEXT,
      UNIQUE(cid, mid)
    );
    CREATE INDEX IF NOT EXISTS outbox_claim ON outbox(cid, status, available_at);
    ''')


def enqueue_handoff(db, cid, mid, result, now):
    """Called only inside the transaction that completes an Agent-validated turn.

    One immutable snapshot per customer/message; an ordinary answer creates no
    event. The model never supplies an event ID, destination, or tenant identity.
    """
    if result.get('action') not in ('handoff', 'escalate'):
        return result
    draft = result.get('handoff_draft')
    if (not isinstance(draft, dict) or draft.get('profile') != result.get('profile')
            or not isinstance(result.get('profile'), dict)
            or result.get('state_revision') != result['profile'].get('revision')
            or not result.get('needs_human') or result.get('external_delivery') is not False):
        raise ValueError('handoff requires a consistent, locally validated draft')
    event_id = event_id_for(cid, mid)
    payload = canonical({'schema': 'handoff.v1', 'event_id': event_id,
                         'customer_scope': cid, 'message_id': mid,
                         'action': result['action'], 'summary': result['answer'],
                         'profile': draft['profile'], 'state_revision': result['state_revision']})
    old = db.execute('SELECT payload FROM outbox WHERE event_id=?', (event_id,)).fetchone()
    if old and old[0] != payload:
        raise ValueError('event identity reused with a different payload')
    if not old:
        db.execute('INSERT INTO outbox(event_id,cid,mid,payload,available_at,created) VALUES(?,?,?,?,?,?)',
                   (event_id, cid, mid, payload, now, now))
    return {**result, 'delivery': {'event_id': event_id, 'status': 'pending',
                                 'destination': 'fictional-local-sink'}}


class OutboxMixin:
    """SQLite-only persistence; cid is already scoped by the existing API auth."""
    supports_outbox = True

    def get_delivery(self, cid, event_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT event_id,mid,status,attempts,created,available_at,lease_until,'
                             'delivered_at,last_error FROM outbox WHERE cid=? AND event_id=?',
                             (cid, event_id)).fetchone()
        return dict(row) if row else None

    def list_deliveries(self, cid, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('delivery limit must be between 1 and 100')
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT event_id,mid,status,attempts,created,available_at,lease_until,'
                              'delivered_at,last_error FROM outbox WHERE cid=? '
                              'ORDER BY created DESC,event_id DESC LIMIT ?', (cid, limit)).fetchall()
        return [dict(row) for row in rows]

    def claim_delivery(self, cid, worker_id, *, now, lease_seconds=30, max_attempts=3):
        valid_time(now)
        positive_number(lease_seconds, 'lease_seconds')
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError('worker_id is required')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError('max_attempts must be between 1 and 100')
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            # A last-attempt process death must not leave an event stuck forever.
            # Delivery may already have happened: dead means unresolved, not unsent.
            db.execute("UPDATE outbox SET status='dead',claim_token=NULL,worker_id=NULL,lease_until=NULL,"
                       "last_error='AttemptsExhausted' WHERE cid=? AND attempts>=? AND "
                       "((status='inflight' AND lease_until<=?) OR status='pending')",
                       (cid, max_attempts, now))
            row = db.execute("SELECT * FROM outbox WHERE cid=? AND attempts<? AND "
                             "((status='pending' AND available_at<=?) OR "
                             "(status='inflight' AND lease_until<=?)) ORDER BY created,event_id LIMIT 1",
                             (cid, max_attempts, now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE outbox SET status='inflight',attempts=attempts+1,lease_until=?,"
                       'claim_token=?,worker_id=? WHERE event_id=? AND cid=?',
                       (now + lease_seconds, token, worker_id, row['event_id'], cid))
            return {'event_id': row['event_id'], 'customer_scope': cid,
                    'payload': json.loads(row['payload']), 'claim_token': token,
                    'attempt': row['attempts'] + 1, 'lease_until': now + lease_seconds}

    def acknowledge_delivery(self, cid, event_id, claim_token, *, now):
        valid_time(now)
        with self.connect() as db:
            cursor = db.execute("UPDATE outbox SET status='delivered',delivered_at=?,last_error=NULL,"
                                'claim_token=NULL,worker_id=NULL,lease_until=NULL '
                                "WHERE cid=? AND event_id=? AND claim_token=? AND status='inflight' AND lease_until>?",
                                (now, cid, event_id, claim_token, now))
            return cursor.rowcount == 1

    def fail_delivery(self, cid, event_id, claim_token, *, now, error, max_attempts=3,
                      backoff_base=1, backoff_cap=60):
        valid_time(now)
        positive_number(backoff_base, 'backoff_base')
        positive_number(backoff_cap, 'backoff_cap')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError('max_attempts must be between 1 and 100')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT attempts FROM outbox WHERE cid=? AND event_id=? AND claim_token=? "
                             "AND status='inflight' AND lease_until>?", (cid, event_id, claim_token, now)).fetchone()
            if row is None:
                return False
            delay = min(backoff_cap, backoff_base * 2 ** (row[0] - 1))
            status = 'dead' if row[0] >= max_attempts else 'pending'
            # Only a bounded exception type is kept; exception messages could contain input data.
            error_type = type(error).__name__[:80]
            db.execute('UPDATE outbox SET status=?,available_at=?,last_error=?,claim_token=NULL,'
                       'worker_id=NULL,lease_until=NULL WHERE cid=? AND event_id=?',
                       (status, now + delay, error_type, cid, event_id))
            return True


class FictionalSink:
    """A separate, persistent, local receiver; never sends to a real channel."""
    def __init__(self, path, customer_scope):
        self.path, self.customer_scope = Path(path), customer_scope
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts '
                       '(event_id TEXT PRIMARY KEY,cid TEXT NOT NULL,payload TEXT NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def deliver(self, event):
        payload = event['payload']
        if (event['customer_scope'] != self.customer_scope
                or payload['customer_scope'] != self.customer_scope
                or payload['event_id'] != event['event_id']):
            raise ValueError('fictional sink scope or event mismatch')
        body = canonical(payload)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT cid,payload FROM receipts WHERE event_id=?',
                             (event['event_id'],)).fetchone()
            if old:
                if old != (self.customer_scope, body):
                    raise ValueError('receipt identity reused with a different payload')
                return {'event_id': event['event_id'], 'duplicate': True}
            db.execute('INSERT INTO receipts VALUES(?,?,?)', (event['event_id'], self.customer_scope, body))
            return {'event_id': event['event_id'], 'duplicate': False}

    def receipt_count(self):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM receipts WHERE cid=?', (self.customer_scope,)).fetchone()[0]


class SimulatedCrash(BaseException):
    """Fault injection bypasses normal failure handling, like an abrupt exit."""


class Dispatcher:
    """Inject a trusted local sink implementing deliver(event); no default network sink."""
    def __init__(self, store, sink, customer_scope, *, worker_id='offline-worker',
                 lease_seconds=30, max_attempts=3, backoff_base=1, backoff_cap=60, clock=time.time):
        if not getattr(store, 'supports_outbox', False):
            raise ValueError('Outbox requires SQLite Store')
        positive_number(lease_seconds, 'lease_seconds')
        positive_number(backoff_base, 'backoff_base')
        positive_number(backoff_cap, 'backoff_cap')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError('max_attempts must be between 1 and 100')
        self.store, self.sink, self.cid, self.worker_id = store, sink, customer_scope, worker_id
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.backoff_base, self.backoff_cap, self.clock = backoff_base, backoff_cap, clock

    def dispatch_one(self, *, crash_after_send=False):
        event = self.store.claim_delivery(self.cid, self.worker_id, now=self.clock(),
            lease_seconds=self.lease_seconds, max_attempts=self.max_attempts)
        if event is None:
            return {'status': 'idle'}
        try:
            receipt = self.sink.deliver(event)
            if not isinstance(receipt, dict) or receipt.get('event_id') != event['event_id']:
                raise ValueError('sink did not acknowledge the same event')
        except Exception as exc:
            accepted = self.store.fail_delivery(self.cid, event['event_id'], event['claim_token'],
                now=self.clock(), error=exc, max_attempts=self.max_attempts,
                backoff_base=self.backoff_base, backoff_cap=self.backoff_cap)
            status = self.store.get_delivery(self.cid, event['event_id'])['status'] if accepted else 'lease-lost'
            return {'event_id': event['event_id'], 'status': status}
        if crash_after_send:
            raise SimulatedCrash('fictional receiver committed; dispatcher exited before acknowledgement')
        accepted = self.store.acknowledge_delivery(self.cid, event['event_id'], event['claim_token'], now=self.clock())
        return {'event_id': event['event_id'], 'status': 'delivered' if accepted else 'lease-lost',
                'duplicate_at_sink': receipt.get('duplicate', False)}
