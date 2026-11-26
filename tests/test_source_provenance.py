from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
pytest.importorskip('coa_common')
from auth.source_credentials import SourceCredential
from auth.source_provenance import SourceProvenance, content_digest

GH_ITEM={'number':5,'base':{'repo':{'id':17}},'title':'Private title','body':'Private body',
         'html_url':'https://github.com/acme/repo/pull/5','updated_at':'2026-01-01T00:00:00Z'}
GL_ITEM={'iid':5,'project_id':17,'title':'Private title','description':'Private body',
         'web_url':'https://gitlab.com/acme/repo/-/merge_requests/5','updated_at':'2026-01-01T00:00:00Z'}
GH_RECORD={'number':5,'repository_id':17,'title':'Private title','body':'Private body',
           'url':'https://github.com/acme/repo/pull/5','updated_at':'2026-01-01T00:00:00Z'}
GL_RECORD={'iid':5,'project_id':17,'title':'Private title','body':'Private body',
           'url':'https://gitlab.com/acme/repo/-/merge_requests/5','updated_at':'2026-01-01T00:00:00Z'}


def proof(provider='github'):
    return {'provider':provider,'project':'acme/repo','scope_id':17,'number':5,
            'digest':content_digest(GH_RECORD if provider=='github' else GL_RECORD)}


@pytest.fixture
def service():
    credentials=Mock();credentials.resolve.return_value=SourceCredential(42,'fake-token')
    return SourceProvenance(credentials),credentials


def test_github_rechecks_scope_and_specific_item_as_authenticated_user(service):
    guard,credentials=service
    with patch('auth.source_provenance.github_private._repository',return_value=(17,'acme/repo')) as repo, patch('auth.source_provenance.github_private._get',return_value=GH_ITEM) as get:
        assert guard.allows('Bearer alice',proof()) is True
    credentials.resolve.assert_called_once_with('Bearer alice','github')
    repo.assert_called_once_with('fake-token','acme/repo')
    get.assert_called_once_with('fake-token','repos/acme/repo/pulls/5')


def test_gitlab_rechecks_scope_and_specific_item_as_authenticated_user(service):
    guard,credentials=service
    with patch('auth.source_provenance.gitlab_private._project',return_value=17) as project, patch('auth.source_provenance.gitlab_private._get',return_value=GL_ITEM) as get:
        assert guard.allows('Bearer bob',proof('gitlab')) is True
    credentials.resolve.assert_called_once_with('Bearer bob','gitlab')
    project.assert_called_once_with('fake-token','acme/repo')
    get.assert_called_once_with('fake-token','projects/17/merge_requests/5')


@pytest.mark.parametrize('bad',[None,{}, {**proof(),'provider':'unknown'}, {**proof(),'project':None},
    {**proof(),'scope_id':True},{**proof(),'scope_id':0},{**proof(),'number':True},{**proof(),'number':0},
    {**proof(),'digest':'0'*63},{**proof(),'digest':'A'*64},{**proof(),'extra':'untrusted'}])
def test_invalid_provenance_never_resolves_credentials(service,bad):
    guard,credentials=service
    assert guard.allows('Bearer alice',bad) is False
    credentials.resolve.assert_not_called()


@pytest.mark.parametrize('provider',['github','gitlab'])
def test_changed_item_denied_even_when_requester_has_access(service,provider):
    guard,_=service
    item=deepcopy(GH_ITEM if provider=='github' else GL_ITEM)
    item['title']='Changed title'
    target='github_private' if provider=='github' else 'gitlab_private'
    scope=(17,'acme/repo') if provider=='github' else 17
    scope_method='_repository' if provider=='github' else '_project'
    with patch(f'auth.source_provenance.{target}.{scope_method}',return_value=scope), patch(f'auth.source_provenance.{target}._get',return_value=item):
        assert guard.allows('Bearer alice',proof(provider)) is False


@pytest.mark.parametrize('provider',['github','gitlab'])
def test_wrong_scope_denies_before_fetching_item(service,provider):
    guard,_=service
    target='github_private' if provider=='github' else 'gitlab_private'
    scope=(18,'acme/repo') if provider=='github' else 18
    scope_method='_repository' if provider=='github' else '_project'
    with patch(f'auth.source_provenance.{target}.{scope_method}',return_value=scope), patch(f'auth.source_provenance.{target}._get') as get:
        assert guard.allows('Bearer alice',proof(provider)) is False
    get.assert_not_called()


def test_response_for_other_item_denied_even_when_digest_matches(service):
    guard,_=service
    item={**GH_ITEM,'number':6,'html_url':'https://github.com/acme/repo/pull/6'}
    record={**GH_RECORD,'number':6,'url':item['html_url']}
    with patch('auth.source_provenance.github_private._repository',return_value=(17,'acme/repo')), patch('auth.source_provenance.github_private._get',return_value=item):
        assert guard.allows('Bearer alice',{**proof(),'digest':content_digest(record)}) is False


def test_gitlab_response_for_other_item_is_denied(service):
    guard,_=service
    item={**GL_ITEM,'iid':6,'web_url':'https://gitlab.com/acme/repo/-/merge_requests/6'}
    record={**GL_RECORD,'iid':6,'url':item['web_url']}
    with patch('auth.source_provenance.gitlab_private._project',return_value=17), patch('auth.source_provenance.gitlab_private._get',return_value=item):
        assert guard.allows('Bearer alice',{**proof('gitlab'),'digest':content_digest(record)}) is False


@pytest.mark.parametrize('stage',['credentials','scope','item'])
def test_revocation_deletion_or_outage_fails_closed(service,stage):
    guard,credentials=service
    if stage=='credentials': credentials.resolve.side_effect=RuntimeError('sensitive')
    with patch('auth.source_provenance.github_private._repository',side_effect=RuntimeError('sensitive') if stage=='scope' else None,return_value=(17,'acme/repo')), patch('auth.source_provenance.github_private._get',side_effect=RuntimeError('sensitive') if stage=='item' else None,return_value=GH_ITEM):
        assert guard.allows('Bearer alice',proof()) is False


def test_digest_is_independent_of_dictionary_order_and_covers_content():
    assert content_digest(GH_RECORD)==content_digest(dict(reversed(list(GH_RECORD.items()))))
    assert content_digest(GH_RECORD)!=content_digest({**GH_RECORD,'body':'changed'})
