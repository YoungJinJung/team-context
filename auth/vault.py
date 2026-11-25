"""Local development vault. Tokens never belong in connector configuration."""
import json
import re
import secrets
import boto3


def client():
    return boto3.client('secretsmanager', endpoint_url='http://127.0.0.1:4568', region_name='us-east-1', aws_access_key_id='local', aws_secret_access_key='local')


def _name(secret_id):
    if not isinstance(secret_id, str) or not re.fullmatch(r'[0-9a-f]{64}', secret_id):
        raise ValueError('Invalid secret ID')
    return 'team-context/credentials/' + secret_id


def create(tokens):
    secret_id = secrets.token_hex(32)
    client().create_secret(Name=_name(secret_id), SecretString=json.dumps(tokens))
    return secret_id


def read(secret_id):
    return json.loads(client().get_secret_value(SecretId=_name(secret_id))['SecretString'])


def replace(secret_id, tokens):
    client().put_secret_value(SecretId=_name(secret_id), SecretString=json.dumps(tokens))


def delete(secret_id):
    client().delete_secret(SecretId=_name(secret_id), ForceDeleteWithoutRecovery=True)
