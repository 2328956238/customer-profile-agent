"""Optional static bearer credentials for the local multi-tenant API demo.

Configuration is operator-owned. Neither a URL nor a request body selects a
tenant. This module deliberately does not implement users, JWTs, or role policy.
"""
import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping


def scoped_customer_id(tenant_id: str, customer_id: str) -> str:
    """Injectively encode the pair, including delimiters and Unicode IDs."""
    pair = json.dumps([tenant_id, customer_id], ensure_ascii=True, separators=(',', ':'))
    return 'tenant:v1:' + base64.urlsafe_b64encode(pair.encode('utf-8')).decode('ascii')


class ApiAuth:
    """Validated, redacted credential configuration; omitted tokens mean demo mode."""

    def __init__(self, tokens_by_tenant: Mapping[str, str] | None = None):
        self._credentials = ()
        if tokens_by_tenant is None:
            return
        error = 'PROFILE_AUTH_TOKENS must map distinct tenant IDs to distinct 32–256 character URL-safe tokens'
        if not isinstance(tokens_by_tenant, Mapping) or not tokens_by_tenant:
            raise ValueError(error)
        credentials = []
        seen = set()
        for tenant, token in tokens_by_tenant.items():
            if (not isinstance(tenant, str) or not 1 <= len(tenant) <= 100
                    or not tenant.strip() or not isinstance(token, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token)):
                raise ValueError(error)
            digest = hashlib.sha256(token.encode('ascii')).digest()
            if digest in seen:
                raise ValueError(error)
            seen.add(digest)
            credentials.append((tenant, digest))
        # Keep only digests; repr and configuration exceptions never contain keys.
        self._credentials = tuple(credentials)

    @property
    def enabled(self) -> bool:
        return bool(self._credentials)

    def __repr__(self) -> str:
        return f'ApiAuth(enabled={self.enabled}, tenants={len(self._credentials)})'

    def tenant_for_token(self, token: str) -> str | None:
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
            return None
        incoming = hashlib.sha256(token.encode('ascii')).digest()
        tenant = None
        for candidate, expected in self._credentials:
            if hmac.compare_digest(incoming, expected):
                tenant = candidate
        return tenant

    @classmethod
    def from_env(cls):
        raw = os.getenv('PROFILE_AUTH_TOKENS')
        if raw is None:
            return cls()

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate configuration key')
                result[key] = value
            return result

        try:
            parsed = json.loads(raw, object_pairs_hook=unique_object)
            # Explicit null, empty objects and blank values must not disable auth.
            if not isinstance(parsed, dict):
                raise ValueError('Expected object')
            return cls(parsed)
        except (ValueError, TypeError):
            raise ValueError('Invalid PROFILE_AUTH_TOKENS; expected a nonempty JSON object of tenant IDs and unique URL-safe tokens (32–256 characters)') from None
