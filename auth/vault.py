"""Local development vault. Tokens never belong in connector configuration."""
import json
import re
import secrets
from local_aws import client as emulator_client
from botocore.exceptions import ClientError


def client():
    return emulator_client('secretsmanager')


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
    try:
        client().delete_secret(SecretId=_name(secret_id), ForceDeleteWithoutRecovery=True)
    except ClientError as error:
        if error.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
