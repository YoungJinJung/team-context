"""Validate delegated Entra access tokens for a future MCP resource server."""
from dataclasses import dataclass
from uuid import UUID

import jwt


@dataclass(frozen=True)
class Requester:
    tenant_id: str
    user_id: str


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError('Invalid identifier')
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError('Invalid identifier')
    return value


class EntraVerifier:
    """Verify user-delegated v2 tokens for one explicitly configured tenant and API."""

    def __init__(self, tenant_id, audience, required_scope):
        self._tenant_id = _uuid(tenant_id)
        if not isinstance(audience, str) or not audience or not isinstance(required_scope, str) or not required_scope or len(required_scope.split()) != 1:
            raise ValueError('Invalid resource configuration')
        self._audience = audience
        self._required_scope = required_scope
        self._issuer = f'https://login.microsoftonline.com/{self._tenant_id}/v2.0'
        self._keys = jwt.PyJWKClient(f'https://login.microsoftonline.com/{self._tenant_id}/discovery/v2.0/keys', timeout=5)

    def verify(self, token):
        """Return a tenant/user identity or None; do not expose authentication errors."""
        if not isinstance(token, str) or not token or len(token) > 16384:
            return None
        try:
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not 1 <= len(header['kid']) <= 128:
                return None
            key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=['RS256'], audience=self._audience,
                issuer=self._issuer, options={'require': ['exp', 'iat', 'nbf', 'iss', 'aud', 'tid', 'oid', 'scp', 'ver'], 'strict_aud': True})
            if claims['tid'] != self._tenant_id or claims['ver'] != '2.0':
                return None
            if not isinstance(claims['scp'], str) or self._required_scope not in claims['scp'].split():
                return None
            return Requester(self._tenant_id, _uuid(claims['oid']))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError, OSError):
            return None
