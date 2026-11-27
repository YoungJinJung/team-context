"""Connect only to the local AWS Emulator with its ephemeral credentials."""
from configparser import ConfigParser
from pathlib import Path

import boto3

EMULATOR_DIR = Path(__file__).resolve().parent / '.aws-emulator' / '9297'


def client(service):
    credentials = ConfigParser()
    with (EMULATOR_DIR / 'emulator_credentials').open() as source:
        credentials.read_file(source)
    return boto3.client(
        service,
        endpoint_url='https://127.0.0.1:9297',
        region_name='us-east-1',
        aws_access_key_id=credentials['default']['aws_access_key_id'],
        aws_secret_access_key=credentials['default']['aws_secret_access_key'],
        verify=str(EMULATOR_DIR / 'ca.pem'),
    )
