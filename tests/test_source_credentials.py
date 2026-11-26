import json
from unittest.mock import Mock, patch

import pytest
pytest.importorskip('coa_common')
from auth.coa_identity import Principal
from auth.link_completion import _secret_name
from auth.source_credentials import SourceCredentials

ALICE=Principal('https://issuer.example','alice')
BOB=Principal('https://issuer.example','bob')
PAYLOAD={'issuer':ALICE.issuer,'subject':ALICE.subject,'provider':'github','provider_user_id':42,
         'access_token':'access-secret','refresh_token':'refresh-secret','expires_at':2000}


@pytest.fixture
def setup():
    verifier=Mock();verifier.verify.return_value=ALICE
    secrets=Mock();secrets.get_secret_value.return_value={'SecretString':json.dumps(PAYLOAD)}
    return SourceCredentials(verifier,secrets,'links/'),verifier,secrets


def test_uses_fresh_coa_principal_and_only_its_provider_secret(setup):
    service,verifier,secrets=setup
    with patch('auth.source_credentials.time.time',return_value=1000):
        credential=service.resolve('Bearer alice','github')
    verifier.verify.assert_called_once_with('Bearer alice')
    secrets.get_secret_value.assert_called_once_with(SecretId=_secret_name('links/',ALICE,'github'))
    assert credential.provider_user_id==42 and credential.access_token=='access-secret'
    assert 'access-secret' not in repr(credential) and 'refresh-secret' not in repr(credential)


@pytest.mark.parametrize('identity,provider',[(None,'github'),(Principal('','alice'),'github'),(Principal('issuer',''),'github'),(ALICE,'other'),(ALICE,None)])
def test_invalid_identity_or_provider_never_reads_secret(setup,identity,provider):
    service,verifier,secrets=setup;verifier.verify.return_value=identity
    with pytest.raises(ValueError,match='^Unable to resolve source credential$'): service.resolve('Bearer fake',provider)
    secrets.get_secret_value.assert_not_called()


@pytest.mark.parametrize('patch_data',[
    {'issuer':'other'}, {'subject':'bob'}, {'provider':'gitlab'}, {'provider_user_id':0},
    {'provider_user_id':True}, {'access_token':''}, {'access_token':'has space'},
    {'refresh_token':''}, {'expires_at':1000}, {'expires_at':True}, {'expires_at':87401},
])
def test_rejects_tampered_or_expired_secrets(setup,patch_data):
    service,_,secrets=setup;secrets.get_secret_value.return_value={'SecretString':json.dumps({**PAYLOAD,**patch_data})}
    with patch('auth.source_credentials.time.time',return_value=1000):
        with pytest.raises(ValueError,match='^Unable to resolve source credential$') as error: service.resolve('Bearer alice','github')
    assert 'access-secret' not in str(error.value)


@pytest.mark.parametrize('raw',[None,'[]','{}','{"subject":"alice","subject":"bob"}','x'*65537,'not-json'])
def test_rejects_malformed_secret_payloads(setup,raw):
    service,_,secrets=setup;secrets.get_secret_value.return_value={'SecretString':raw}
    with pytest.raises(ValueError,match='^Unable to resolve source credential$'): service.resolve('Bearer alice','github')


def test_read_failure_and_forged_identity_do_not_leak_or_fall_back(setup):
    service,verifier,secrets=setup
    secrets.get_secret_value.side_effect=RuntimeError('secret-aws-error')
    with pytest.raises(ValueError,match='^Unable to resolve source credential$') as error: service.resolve('Bearer alice','github')
    assert 'secret-aws-error' not in str(error.value)
    secrets.get_secret_value.reset_mock();verifier.verify.return_value=BOB
    secrets.get_secret_value.side_effect=None;secrets.get_secret_value.return_value={'SecretString':json.dumps(PAYLOAD)}
    with patch('auth.source_credentials.time.time',return_value=1000):
        with pytest.raises(ValueError): service.resolve('Bearer bob','github')
    secrets.get_secret_value.assert_called_once_with(SecretId=_secret_name('links/',BOB,'github'))


def test_gitlab_uses_separate_name_and_matching_provider(setup):
    service,_,secrets=setup
    secrets.get_secret_value.return_value={'SecretString':json.dumps({**PAYLOAD,'provider':'gitlab'})}
    with patch('auth.source_credentials.time.time',return_value=1000):
        assert service.resolve('Bearer alice','gitlab').provider_user_id==42
    secrets.get_secret_value.assert_called_once_with(SecretId=_secret_name('links/',ALICE,'gitlab'))


@pytest.mark.parametrize('prefix',[None,'','bad prefix/','x'*301])
def test_invalid_secret_prefix_is_rejected(prefix):
    with pytest.raises(ValueError,match='^Invalid credential configuration$'):
        SourceCredentials(Mock(),Mock(),prefix)


@pytest.mark.parametrize('expires_at,allowed',[(1000,False),(1001,True),(87400,True),(87401,False)])
def test_expiry_must_be_in_the_supported_window(setup,expires_at,allowed):
    service,_,secrets=setup
    secrets.get_secret_value.return_value={'SecretString':json.dumps({**PAYLOAD,'expires_at':expires_at})}
    with patch('auth.source_credentials.time.time',return_value=1000):
        if allowed:
            assert service.resolve('Bearer alice','github').provider_user_id==42
        else:
            with pytest.raises(ValueError): service.resolve('Bearer alice','github')
