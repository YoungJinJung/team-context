"""Resolve provider credentials only for the freshly verified COA caller.

This internal component is not an MCP tool or HTTP handler. Callers must never
serialize its result or log it. It does not renew tokens or prove source access.
"""
from dataclasses import dataclass, field
import json
import re
import time

from auth.coa_identity import Principal, _nonempty
from auth.link_completion import _secret_name, _valid_secret


@dataclass(frozen=True)
class SourceCredential:
    provider_user_id: int
    access_token: str = field(repr=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate credential field')
        result[key] = value
    return result


class SourceCredentials:
    """Read the caller's unexpired credential from trusted server configuration."""

    def __init__(self, verifier, secrets_manager, prefix):
        if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9/_+=.@-]{1,300}', prefix):
            raise ValueError('Invalid credential configuration')
        self._verifier = verifier
        self._secrets = secrets_manager
        self._prefix = prefix

    def resolve(self, authorization, provider):
        """Return an internal credential, or fail closed without exposing secrets."""
        try:
            principal = self._verifier.verify(authorization)
            if not isinstance(principal, Principal) or not _nonempty(principal.issuer) or not _nonempty(principal.subject) or provider not in ('github', 'gitlab'):
                raise ValueError('Invalid requester')
            response = self._secrets.get_secret_value(SecretId=_secret_name(self._prefix, principal, provider))
            raw = response['SecretString']
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
                raise ValueError('Invalid credential')
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or data.get('issuer') != principal.issuer or data.get('subject') != principal.subject or data.get('provider') != provider:
                raise ValueError('Invalid credential')
            if type(data.get('provider_user_id')) is not int or data['provider_user_id'] <= 0 or not _valid_secret(data.get('access_token')) or not _valid_secret(data.get('refresh_token')):
                raise ValueError('Invalid credential')
            now = time.time()
            expiry = data.get('expires_at')
            if type(expiry) is not int or not now < expiry <= now + 86400:
                raise ValueError('Expired credential')
            return SourceCredential(data['provider_user_id'], data['access_token'])
        except Exception:
            raise ValueError('Unable to resolve source credential') from None
