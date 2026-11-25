import json
from unittest.mock import Mock, patch
import pytest
from auth import vault


ID = 'a' * 64
NAME = 'team-context/credentials/' + ID
TOKENS = {'access_token': 'access', 'refresh_token': 'refresh'}


def test_client_uses_only_emulator():
    with patch('auth.vault.boto3.client') as factory:
        vault.client()
    factory.assert_called_once_with('secretsmanager', endpoint_url='http://127.0.0.1:4568', region_name='us-east-1', aws_access_key_id='local', aws_secret_access_key='local')


@pytest.mark.parametrize('identifier', ['', 'a' * 63, 'a' * 65, 'G' * 64, '../path', None, 12])
def test_rejects_invalid_id(identifier):
    with patch('auth.vault.client') as client:
        with pytest.raises(ValueError): vault.read(identifier)
        client.return_value.get_secret_value.assert_not_called()


def test_create_read_rotate_and_delete():
    store = Mock()
    store.get_secret_value.return_value = {'SecretString': json.dumps(TOKENS)}
    with patch('auth.vault.client', return_value=store), patch('auth.vault.secrets.token_hex', return_value=ID) as random:
        assert vault.create(TOKENS) == ID
        assert vault.read(ID) == TOKENS
        vault.replace(ID, TOKENS)
        vault.delete(ID)
    random.assert_called_once_with(32)
    store.create_secret.assert_called_once_with(Name=NAME, SecretString=json.dumps(TOKENS))
    store.get_secret_value.assert_called_once_with(SecretId=NAME)
    store.put_secret_value.assert_called_once_with(SecretId=NAME, SecretString=json.dumps(TOKENS))
    store.delete_secret.assert_called_once_with(SecretId=NAME, ForceDeleteWithoutRecovery=True)


def test_creation_failure_propagates():
    with patch('auth.vault.client') as client:
        client.return_value.create_secret.side_effect = OSError('unavailable')
        with pytest.raises(OSError): vault.create(TOKENS)
