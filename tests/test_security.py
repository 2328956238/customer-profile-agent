"""Offline identity, permission and audit contracts; no real user data."""
from dataclasses import FrozenInstanceError
import json
import secrets
import sqlite3

import pytest

from customer_profile_agent.profile_store import Store
from customer_profile_agent.security import Principal, SecurityStore, require


@pytest.fixture
def security(tmp_path):
    return SecurityStore(tmp_path / 'shared.db')


def issue(security, *, tenant='fictional-shop', actor='operator', role='agent'):
    token = secrets.token_urlsafe(32)
    token_id = security.bootstrap_token(token, tenant, actor, role)
    return token, token_id


@pytest.mark.parametrize('role,allowed', [
    ('viewer', {'profile:read', 'knowledge:read'}),
    ('agent', {'profile:read', 'profile:write', 'knowledge:read', 'lead:score'}),
    ('admin', {'profile:read', 'profile:write', 'knowledge:read', 'knowledge:write',
               'lead:score', 'metrics:read', 'audit:read'}),
])
def test_server_defined_permission_matrix(security, role, allowed):
    token, _ = issue(security, role=role)
    principal = security.authenticate(token)
    assert principal == Principal('operator', 'fictional-shop', role)
    for permission in ['profile:read', 'profile:write', 'knowledge:read', 'knowledge:write',
                       'lead:score', 'metrics:read', 'audit:read', 'unknown:operation']:
        if permission in allowed:
            assert require(principal, permission) is None
        else:
            with pytest.raises(PermissionError, match='Permission denied'):
                require(principal, permission)


def test_request_dictionary_cannot_supply_a_privileged_principal(security):
    token, _ = issue(security, role='viewer')
    fake_request = {'actor_id': 'operator', 'tenant_id': 'other-shop', 'role': 'admin'}
    with pytest.raises(PermissionError):
        require(fake_request, 'knowledge:write')
    actual = security.authenticate(token)
    assert actual.role == 'viewer' and actual.tenant_id == 'fictional-shop'
    with pytest.raises(FrozenInstanceError):
        actual.role = 'admin'
    with pytest.raises(ValueError, match='role'):
        Principal('operator', 'fictional-shop', 'super-admin')
    with pytest.raises(PermissionError):
        security.record_audit(fake_request, 'profile.read', 'customer-1', 'success')


def test_credential_hash_only_and_shared_profile_database(security):
    token, token_id = issue(security)
    profile = Store(security.path)
    assert profile.load('fictional-customer')['revision'] == 0
    assert SecurityStore(profile.path).authenticate(token).actor_id == 'operator'
    assert security.bootstrap_token(token, 'fictional-shop', 'operator', 'agent') == token_id
    with sqlite3.connect(security.path) as db:
        stored = db.execute('SELECT digest FROM security_tokens').fetchone()[0]
        assert isinstance(stored, bytes) and len(stored) == 32
        dumped = '\n'.join(db.iterdump())
    assert token not in dumped
    assert token not in repr(security.authenticate(token))


def test_revocation_is_visible_to_existing_and_restarted_stores(security):
    token, token_id = issue(security)
    reader = SecurityStore(security.path)
    assert reader.authenticate(token) is not None
    assert security.revoke_token_id(token_id)
    assert reader.authenticate(token) is None
    assert SecurityStore(security.path).authenticate(token) is None
    assert not security.revoke_token(token)
    with pytest.raises(ValueError, match='reactivated'):
        security.bootstrap_token(token, 'fictional-shop', 'operator', 'agent')
    fresh, _ = issue(security)
    assert security.revoke_token(fresh)
    assert security.authenticate(fresh) is None


def test_role_change_revokes_all_old_tokens_only_for_that_tenant_actor(security):
    first, _ = issue(security, role='admin')
    second, _ = issue(security, role='admin')
    other_actor, _ = issue(security, actor='other-operator', role='admin')
    other_tenant, _ = issue(security, tenant='other-shop', role='admin')
    replacement, _ = issue(security, role='viewer')
    assert security.authenticate(first) is None
    assert security.authenticate(second) is None
    assert security.authenticate(other_actor).role == 'admin'
    assert security.authenticate(other_tenant).role == 'admin'
    assert security.authenticate(replacement).role == 'viewer'
    with pytest.raises(PermissionError):
        require(security.authenticate(replacement), 'profile:write')
    assert security.revoke_actor('fictional-shop', 'operator') == 1
    assert security.authenticate(replacement) is None
    assert security.authenticate(other_tenant) is not None


def test_existing_token_cannot_be_rebound_to_another_identity_or_role(security):
    token, _ = issue(security)
    for tenant, actor, role in [('other-shop', 'operator', 'agent'),
                               ('fictional-shop', 'other-operator', 'agent'),
                               ('fictional-shop', 'operator', 'admin')]:
        with pytest.raises(ValueError, match='reassigned'):
            security.bootstrap_token(token, tenant, actor, role)
    assert security.authenticate(token) == Principal('operator', 'fictional-shop', 'agent')


@pytest.mark.parametrize('invalid', [None, '', 'short', 'x' * 257, 'x' * 31 + '+', 'x' * 31 + '\n'])
def test_invalid_tokens_fail_closed_without_reflecting_input(security, invalid):
    assert security.authenticate(invalid) is None
    assert security.revoke_token(invalid) is False
    with pytest.raises(ValueError, match='token must contain'):
        security.bootstrap_token(invalid, 'fictional-shop', 'operator', 'agent')


@pytest.mark.parametrize('invalid', ['', ' ', ' leading', 'trailing ', 'x' * 101, 'control\nvalue'])
def test_identity_validation_rejects_unbounded_or_ambiguous_ids(security, invalid):
    token = secrets.token_urlsafe(32)
    with pytest.raises(ValueError, match='tenant_id'):
        security.bootstrap_token(token, invalid, 'operator', 'agent')
    with pytest.raises(ValueError, match='actor_id'):
        security.bootstrap_token(token, 'fictional-shop', invalid, 'agent')


def test_audit_is_tenant_scoped_and_keyset_pages_do_not_repeat(security):
    actor_a = Principal('operator', 'shop-a', 'admin')
    actor_b = Principal('operator', 'shop-b', 'admin')
    expected_a = []
    for n in range(5):
        expected_a.append(security.record_audit(actor_a, 'profile.read', f'customer-{n}', 'success'))
        security.record_audit(actor_b, 'profile.read', f'other-{n}', 'success')
    first = security.list_audit('shop-a', limit=2)
    # A concurrent new entry does not shift an existing cursor.
    security.record_audit(actor_a, 'profile.read', 'new-entry', 'success')
    second = security.list_audit('shop-a', limit=2, before_id=first[-1]['id'])
    third = security.list_audit('shop-a', limit=2, before_id=second[-1]['id'])
    assert [row['id'] for row in first + second + third] == expected_a[::-1]
    assert all(row['tenant_id'] == 'shop-a' for row in first + second + third)
    assert security.list_audit('missing-shop') == []
    assert all(row['resource_id'].startswith('other-') for row in security.list_audit('shop-b'))


def test_audit_drops_tokens_conversations_and_arbitrary_nested_metadata(security):
    token, _ = issue(security)
    principal = security.authenticate(token)
    conversation = 'Fictional customer message that must never enter the audit database'
    details = {'token': token, 'authorization': 'Bearer ' + token, 'message': conversation,
               'nested': {'response': conversation}, 'count': conversation, 'role': token,
               'result_count': 3, 'changed': True, 'latency_ms': 2.5, 'reason': 'conflict',
               'profile': {'customer': conversation}, 'request': conversation}
    security.record_audit(principal, 'profile.update', 'customer-1', 'attempt', details)
    security.record_audit(principal, 'profile.update', 'customer-1', 'failed')
    rows = security.list_audit(principal.tenant_id)
    assert rows[1]['details'] == {'result_count': 3, 'changed': True, 'latency_ms': 2.5, 'reason': 'conflict'}
    assert token not in json.dumps(rows) and conversation not in json.dumps(rows)
    with sqlite3.connect(security.path) as db:
        dumped = '\n'.join(db.iterdump())
    assert token not in dumped and conversation not in dumped
    with pytest.raises(ValueError, match='opaque identifier'):
        security.record_audit(principal, 'profile.update', conversation, 'success')
    with pytest.raises(ValueError, match='credential'):
        security.record_audit(principal, 'profile.update', token, 'success')


@pytest.mark.parametrize('kwargs', [{'limit': 0}, {'limit': 101}, {'limit': True},
                                   {'before_id': 0}, {'before_id': True}, {'before_id': '1'}])
def test_audit_pagination_bounds(security, kwargs):
    with pytest.raises(ValueError):
        security.list_audit('fictional-shop', **kwargs)


def test_audit_no_implicit_business_success_and_bounded_safe_metadata(security):
    principal = Principal('operator', 'fictional-shop', 'agent')
    security.record_audit(principal, 'profile.update', 'customer-1', 'attempt',
                          {'count': True, 'latency_ms': float('inf'), 'attempts': -1,
                           'reason': 'arbitrary text', 'changed': 'true'})
    row = security.list_audit('fictional-shop')[0]
    assert row['outcome'] == 'attempt' and row['details'] == {}
    with pytest.raises(ValueError, match='outcome'):
        security.record_audit(principal, 'profile.update', 'customer-1', 'arbitrary text')
