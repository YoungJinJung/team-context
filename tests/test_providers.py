from dataclasses import replace

import pytest

from auth.providers import PROVIDERS, register
from auth import session_store


def test_registry_is_immutable_and_has_isolated_bindings():
    assert set(PROVIDERS) == {'linear', 'teams', 'gitlab', 'github'}
    with pytest.raises(TypeError):
        PROVIDERS['new'] = PROVIDERS['linear']
    session = 'a' * 43
    assert len({session_store._key(session, p.namespace) for p in PROVIDERS.values()}) == 4


@pytest.mark.parametrize('changes', [
    {'id': 'github'},
    {'id': 'bad/name'},
    {'cookie': 'team_context_session'},
    {'cookie': 'bad;cookie'},
    {'namespace': None},
    {'namespace': 'github'},
    {'session_name': 'SESSIONS'},
    {'session_name': 'STORAGE_LOCK'},
    {'callback': 'https://attacker.example/oauth/gitlab/callback'},
    {'authorize_origin': 'http://gitlab.com'},
    {'authorize_origin': 'https://gitlab.com@attacker.example'},
    {'authorize_origin': 'https://gitlab.com:444'},
    {'authorize_origin': 'https://gitlab.com/path'},
    {'authorize_origin': 'https://gitlab.com?redirect=attacker'},
    {'authorize_origin': 'https://gitlab.com#fragment'},
])
def test_registry_rejects_unsafe_or_conflicting_providers(changes):
    with pytest.raises(ValueError):
        register([PROVIDERS['linear'], PROVIDERS['github'], replace(PROVIDERS['gitlab'], **changes)])


def test_register_new_trusted_provider_without_route_changes():
    provider = replace(PROVIDERS['gitlab'], id='example', cookie='team_context_example_session',
                       namespace='example', session_name='EXAMPLE_SESSIONS', display_name='Example',
                       callback='http://127.0.0.1:8765/oauth/example/callback',
                       authorize_origin='https://example.com')
    result = register([*PROVIDERS.values(), provider])
    assert result['example'] == provider
