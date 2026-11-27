from unittest.mock import patch

import pytest
import local_aws


def test_client_uses_only_loopback_and_emulator_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(local_aws, 'EMULATOR_DIR', tmp_path)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'real-credentials-must-not-be-used')
    (tmp_path / 'emulator_credentials').write_text('[default]\naws_access_key_id = test-key\naws_secret_access_key = test-secret\n')
    with patch('local_aws.boto3.client') as factory:
        local_aws.client('s3')
    factory.assert_called_once_with('s3', endpoint_url='https://127.0.0.1:9297', region_name='us-east-1', aws_access_key_id='test-key', aws_secret_access_key='test-secret', verify=str(tmp_path / 'ca.pem'))


def test_missing_credentials_never_falls_back_to_real_aws(tmp_path, monkeypatch):
    monkeypatch.setattr(local_aws, 'EMULATOR_DIR', tmp_path)
    with patch('local_aws.boto3.client') as factory, pytest.raises(FileNotFoundError):
        local_aws.client('s3')
    factory.assert_not_called()
