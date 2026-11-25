"""Local-only, hashed browser-session references in DynamoDB."""
import hashlib
import re
import time
from storage import clients


def _key(session):
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', session):
        return None
    return 'session:' + hashlib.sha256(session.encode()).hexdigest()


def put(session, secret_id):
    key = _key(session)
    if key is None:
        raise ValueError('Invalid session')
    _, db = clients()
    db.put_item(TableName='team-context-sync', Item={'connection_id': {'S': key}, 'secret_id': {'S': secret_id}, 'expires_at': {'N': str(int(time.time()) + 2592000)}})


def get(session):
    key = _key(session)
    if key is None:
        return None
    _, db = clients()
    item = db.get_item(TableName='team-context-sync', Key={'connection_id': {'S': key}}, ConsistentRead=True).get('Item')
    if not item or int(item['expires_at']['N']) <= time.time():
        return None
    return item['secret_id']['S']


def remove(session):
    key = _key(session)
    if key is None:
        raise ValueError('Invalid session')
    _, db = clients()
    db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': key}})
