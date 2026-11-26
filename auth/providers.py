"""Explicit, trusted OAuth provider registrations for the local server."""
from dataclasses import dataclass
import re
from types import MappingProxyType
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Provider:
    id: str
    cookie: str
    namespace: str | None
    session_name: str
    display_name: str
    client_env: str
    callback: str
    authorize_origin: str
    missing_config: str
    extra_env: tuple[str, ...] = ()


def register(providers):
    """Reject collisions and unsafe routing before accepting providers."""
    result = {}
    cookies = set()
    for provider in providers:
        if not re.fullmatch(r'[a-z][a-z0-9-]*', provider.id):
            raise ValueError('Invalid provider id')
        if not re.fullmatch(r'team_context_[a-z0-9_]+', provider.cookie):
            raise ValueError('Invalid provider cookie')
        if provider.session_name != ('SESSIONS' if provider.id == 'linear' else provider.id.upper().replace('-', '_') + '_SESSIONS'):
            raise ValueError('Invalid session name')
        if provider.namespace != (None if provider.id == 'linear' else provider.id):
            raise ValueError('Invalid namespace binding')
        if provider.callback != f'http://127.0.0.1:8765/oauth/{provider.id}/callback':
            raise ValueError('Invalid callback')
        origin = urlsplit(provider.authorize_origin)
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.port or origin.path or origin.query or origin.fragment:
            raise ValueError('Invalid authorization origin')
        if provider.id in result or provider.cookie in cookies:
            raise ValueError('Duplicate provider registration')
        result[provider.id] = provider
        cookies.add(provider.cookie)
    return MappingProxyType(result)


PROVIDERS = register((
    Provider('linear', 'team_context_session', None, 'SESSIONS', 'Linear', 'LINEAR_CLIENT_ID', 'http://127.0.0.1:8765/oauth/linear/callback', 'https://linear.app', 'Set LINEAR_CLIENT_ID to your Linear OAuth app client ID.'),
    Provider('teams', 'team_context_teams_session', 'teams', 'TEAMS_SESSIONS', 'Teams', 'TEAMS_CLIENT_ID', 'http://127.0.0.1:8765/oauth/teams/callback', 'https://login.microsoftonline.com', 'Set TEAMS_CLIENT_ID to your Entra app client ID.'),
    Provider('gitlab', 'team_context_gitlab_session', 'gitlab', 'GITLAB_SESSIONS', 'GitLab', 'GITLAB_CLIENT_ID', 'http://127.0.0.1:8765/oauth/gitlab/callback', 'https://gitlab.com', 'Set GITLAB_CLIENT_ID to your GitLab OAuth app ID.'),
    Provider('github', 'team_context_github_session', 'github', 'GITHUB_SESSIONS', 'GitHub', 'GITHUB_CLIENT_ID', 'http://127.0.0.1:8765/oauth/github/callback', 'https://github.com', 'Set GITHUB_CLIENT_ID and GITHUB_APP_SECRET_ID.', ('GITHUB_APP_SECRET_ID',)),
))
