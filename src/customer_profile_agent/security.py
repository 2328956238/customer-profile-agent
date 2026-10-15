"""Local opaque API tokens, server-owned RBAC and metadata-only operation audit.

This module is not a password, JWT, SSO, or user-registration implementation.
Only trusted local operators bootstrap identities. HTTP callers must never build
a Principal from a request body, header, or query parameter.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType
import uuid


ROLE_PERMISSIONS = MappingProxyType({
    'viewer': frozenset({'profile:read', 'knowledge:read'}),
    'agent': frozenset({'profile:read', 'profile:write', 'knowledge:read', 'lead:score'}),
    'admin': frozenset({'profile:read', 'profile:write', 'knowledge:read',
                        'knowledge:write', 'lead:score', 'metrics:read', 'audit:read'}),
})
_TOKEN = re.compile(r'[A-Za-z0-9_-]{32,256}\Z')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z')
_RESOURCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./:=+-]{0,2047}\Z')
_OUTCOMES = frozenset({'attempt', 'success', 'denied', 'failed', 'failure', 'error', 'not_found'})
_COUNT_DETAILS = frozenset({'count', 'result_count', 'matched_count', 'revision',
                            'before_revision', 'after_revision', 'status_code', 'attempts'})
_BOOL_DETAILS = frozenset({'changed', 'retryable', 'dry_run'})
_REASONS = frozenset({'permission_denied', 'not_found', 'invalid_input', 'conflict',
                      'storage_error', 'revoked', 'role_changed'})


def _identity(value, name):
    if (not isinstance(value, str) or not 1 <= len(value) <= 100
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(name + ' must contain 1–100 characters without surrounding whitespace or controls')
    return value


def _role(value):
    if not isinstance(value, str) or value not in ROLE_PERMISSIONS:
        raise ValueError('role must be viewer, agent, or admin')
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    actor_id: str
    tenant_id: str
    role: str

    def __post_init__(self):
        _identity(self.actor_id, 'actor_id')
        _identity(self.tenant_id, 'tenant_id')
        _role(self.role)


def require(principal, permission):
    """Authorize an authenticated snapshot; authenticate afresh on each request.

    A Principal is an internal value, not a cryptographic credential. Resource
    lookup must additionally use principal.tenant_id. This function cannot make
    a request-supplied role or an arbitrary in-process object trustworthy.
    """
    if (type(principal) is not Principal or not isinstance(permission, str)
            or permission not in ROLE_PERMISSIONS.get(principal.role, ())):
        raise PermissionError('Permission denied')


def _digest(token):
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode('ascii')).digest()


def _details(details):
    """Only numeric counters, booleans and fixed enums; never arbitrary strings."""
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise ValueError('audit details must be a mapping')
    safe = {}
    for key, value in details.items():
        if key in _COUNT_DETAILS and type(value) is int and 0 <= value <= 10**12:
            safe[key] = value
        elif key in _BOOL_DETAILS and type(value) is bool:
            safe[key] = value
        elif (key == 'latency_ms' and type(value) in (int, float)
              and math.isfinite(value) and 0 <= value <= 10**12):
            safe[key] = value
        elif key == 'reason' and isinstance(value, str) and value in _REASONS:
            safe[key] = value
        elif key == 'role' and isinstance(value, str) and value in ROLE_PERMISSIONS:
            safe[key] = value
        elif key == 'trace_id' and isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value):
            safe[key] = value
    return safe


class SecurityStore:
    """SQLite storage can share the profile database; table names are prefixed.

    Bootstrap, revocation and raw tenant-based audit listing are trusted local
    operations, not endpoints. Bind HTTP audit reads to the authenticated tenant
    and require audit:read before calling list_audit.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS security_actors (
              tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL, role TEXT NOT NULL,
              revision INTEGER NOT NULL, PRIMARY KEY(tenant_id,actor_id)
            );
            CREATE TABLE IF NOT EXISTS security_tokens (
              token_id TEXT PRIMARY KEY, digest BLOB NOT NULL UNIQUE,
              tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL,
              actor_revision INTEGER NOT NULL, created REAL NOT NULL, revoked_at REAL
            );
            CREATE INDEX IF NOT EXISTS security_tokens_actor
              ON security_tokens(tenant_id,actor_id);
            CREATE TABLE IF NOT EXISTS security_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
              actor_id TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
              resource_id TEXT NOT NULL, outcome TEXT NOT NULL, details TEXT NOT NULL,
              created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS security_audit_tenant
              ON security_audit(tenant_id,id DESC);
            ''')

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bootstrap_token(self, token, tenant_id, actor_id, role):
        """Return an opaque token ID; never return/store the input token.

        Same active token and identity is idempotent. Tokens cannot be moved to
        another identity or revived after revocation. A changed actor role
        revokes every old token for that tenant/actor before issuing the new one.
        """
        principal = Principal(actor_id, tenant_id, role)
        digest = _digest(token)
        if digest is None:
            raise ValueError('token must contain 32–256 URL-safe characters')
        with self._db(write=True) as db:
            existing = db.execute('SELECT * FROM security_tokens WHERE digest=?', (digest,)).fetchone()
            actor = db.execute('SELECT role,revision FROM security_actors WHERE tenant_id=? AND actor_id=?',
                               (tenant_id, actor_id)).fetchone()
            if existing:
                if (existing['tenant_id'] == tenant_id and existing['actor_id'] == actor_id
                        and existing['revoked_at'] is None and actor and actor['role'] == role
                        and existing['actor_revision'] == actor['revision']):
                    return existing['token_id']
                raise ValueError('token cannot be reassigned or reactivated; generate a new token')
            now = time.time()
            if actor is None:
                revision = 1
                db.execute('INSERT INTO security_actors VALUES(?,?,?,?)',
                           (tenant_id, actor_id, principal.role, revision))
            elif actor['role'] != role:
                revision = actor['revision'] + 1
                db.execute('UPDATE security_actors SET role=?,revision=? WHERE tenant_id=? AND actor_id=?',
                           (role, revision, tenant_id, actor_id))
                db.execute('UPDATE security_tokens SET revoked_at=? WHERE tenant_id=? AND actor_id=? '
                           'AND revoked_at IS NULL', (now, tenant_id, actor_id))
            else:
                revision = actor['revision']
            token_id = uuid.uuid4().hex
            db.execute('INSERT INTO security_tokens VALUES(?,?,?,?,?,?,NULL)',
                       (token_id, digest, tenant_id, actor_id, revision, now))
            return token_id

    def authenticate(self, token):
        digest = _digest(token)
        if digest is None:
            return None
        with self._db() as db:
            row = db.execute('SELECT a.actor_id,a.tenant_id,a.role FROM security_tokens t '
                             'JOIN security_actors a ON a.tenant_id=t.tenant_id AND a.actor_id=t.actor_id '
                             'WHERE t.digest=? AND t.revoked_at IS NULL AND t.actor_revision=a.revision',
                             (digest,)).fetchone()
        return Principal(row['actor_id'], row['tenant_id'], row['role']) if row else None

    def revoke_token(self, token):
        digest = _digest(token)
        if digest is None:
            return False
        with self._db(write=True) as db:
            return db.execute('UPDATE security_tokens SET revoked_at=? WHERE digest=? AND revoked_at IS NULL',
                              (time.time(), digest)).rowcount == 1

    def revoke_token_id(self, token_id):
        if not isinstance(token_id, str) or re.fullmatch(r'[a-f0-9]{32}', token_id) is None:
            return False
        with self._db(write=True) as db:
            return db.execute('UPDATE security_tokens SET revoked_at=? WHERE token_id=? AND revoked_at IS NULL',
                              (time.time(), token_id)).rowcount == 1

    def revoke_actor(self, tenant_id, actor_id):
        """Trusted operator removal: invalidate all existing tokens, including cached versions."""
        _identity(tenant_id, 'tenant_id')
        _identity(actor_id, 'actor_id')
        with self._db(write=True) as db:
            db.execute('UPDATE security_actors SET revision=revision+1 WHERE tenant_id=? AND actor_id=?',
                       (tenant_id, actor_id))
            return db.execute('UPDATE security_tokens SET revoked_at=? WHERE tenant_id=? AND actor_id=? '
                              'AND revoked_at IS NULL', (time.time(), tenant_id, actor_id)).rowcount

    def record_audit(self, principal, action, resource_id, outcome, details=None):
        if type(principal) is not Principal:
            raise PermissionError('Authenticated principal required')
        if not isinstance(action, str) or _LABEL.fullmatch(action) is None:
            raise ValueError('audit action must be a server-defined operation label')
        if not isinstance(resource_id, str) or _RESOURCE.fullmatch(resource_id) is None:
            raise ValueError('audit resource_id must be an opaque identifier, never conversation text')
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ValueError('unsupported audit outcome')
        safe = _details(details)
        with self._db(write=True) as db:
            # Also reject accidental placement of an issued bearer token in an ID/label.
            for value in (action, resource_id):
                digest = _digest(value)
                if digest is not None and db.execute('SELECT 1 FROM security_tokens WHERE digest=?',
                                                     (digest,)).fetchone():
                    raise ValueError('credential cannot be used as audit metadata')
            cursor = db.execute('INSERT INTO security_audit '
                                '(tenant_id,actor_id,role,action,resource_id,outcome,details,created) '
                                'VALUES(?,?,?,?,?,?,?,?)',
                                (principal.tenant_id, principal.actor_id, principal.role, action,
                                 resource_id, outcome, json.dumps(safe, sort_keys=True), time.time()))
            return cursor.lastrowid

    def list_audit(self, tenant_id, limit=50, before_id=None):
        _identity(tenant_id, 'tenant_id')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('audit limit must be between 1 and 100')
        if before_id is not None and (type(before_id) is not int or not 0 < before_id <= 9223372036854775807):
            raise ValueError('before_id must be a positive integer')
        query = 'SELECT * FROM security_audit WHERE tenant_id=?'
        args = [tenant_id]
        if before_id is not None:
            query += ' AND id<?'
            args.append(before_id)
        query += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        with self._db() as db:
            rows = db.execute(query, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['details'] = json.loads(item['details'])
            result.append(item)
        return result
