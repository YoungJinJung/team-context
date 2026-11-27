"""Derive an account-linking principal from a COA-verified user token."""
from dataclasses import dataclass
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

from coa_common.auth import build_token_authorizer


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str


def _nonempty(value):
    return isinstance(value, str) and bool(value) and len(value) <= 255 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


class CoaIdentity:
    """Verify delegated user tokens using COA's configured token authorizer.

    For Cognito, audience is the app client ID. Resource-bound tokens with a
    different audience are denied until both values can be configured.
    """

    def __init__(self, issuer, audience, region, scope, mode, auth_enabled):
        if auth_enabled is not True or not all(_nonempty(value) for value in (issuer, audience, region, scope)) or len(scope.split()) != 1:
            raise ValueError('Invalid authentication configuration')
        url = urlsplit(issuer)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port or url.query or url.fragment:
            raise ValueError('Invalid issuer')
        self._tenant = None
        if mode == 'entra':
            parts = url.path.split('/')
            try:
                if url.netloc != 'login.microsoftonline.com' or len(parts) != 3 or parts[2] != 'v2.0' or str(UUID(parts[1])) != parts[1]:
                    raise ValueError('Invalid issuer')
            except (ValueError, AttributeError):
                raise ValueError('Invalid issuer') from None
            self._tenant = parts[1]
        elif mode == 'cognito':
            if url.netloc != f'cognito-idp.{region}.amazonaws.com' or not re.fullmatch('/' + re.escape(region) + r'_[A-Za-z0-9]+', url.path):
                raise ValueError('Invalid issuer')
        else:
            raise ValueError('Invalid identity provider')
        self._issuer = issuer
        self._client_id = audience if mode == 'cognito' else None
        self._scope = scope
        self._mode = mode
        self._authorizer = build_token_authorizer(issuer=issuer, region=region, client_id=audience)

    def verify(self, authorization):
        """Return an issuer-qualified subject, or None for an untrusted request."""
        if not isinstance(authorization, str) or not authorization.startswith('Bearer ') or len(authorization) > 16384:
            return None
        try:
            claims = self._authorizer.verify_claims(authorization)
            if not isinstance(claims, dict) or claims.get('iss') != self._issuer or not _nonempty(claims.get('sub')):
                return None
            now = time.time()
            if type(claims.get('exp')) is not int or claims['exp'] <= now:
                return None
            if any(name in claims and (type(claims[name]) is not int or claims[name] > now) for name in ('iat', 'nbf')):
                return None
            if self._mode == 'entra':
                if claims.get('ver') != '2.0' or claims.get('tid') != self._tenant or not _nonempty(claims.get('oid')):
                    return None
                try:
                    if str(UUID(claims['oid'])) != claims['oid']:
                        return None
                except (ValueError, AttributeError):
                    return None
                scopes = claims.get('scp')
            else:
                if claims.get('token_use') != 'access' or claims.get('client_id') != self._client_id or not _nonempty(claims.get('username')):
                    return None
                scopes = claims.get('scope')
            if not isinstance(scopes, str) or self._scope not in scopes.split():
                return None
            return Principal(self._issuer, claims['sub'])
        except Exception:
            return None
