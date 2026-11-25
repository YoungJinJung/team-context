"""Local-only, hashed browser-session references in DynamoDB."""
import hashlib
import re
import time
from storage import clients


def _key(session, namespace=None):
    if namespace not in (None, 'teams', 'gitlab', 'github'):
        raise ValueError('Invalid namespace')
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', session):
        return None
    prefix = 'session:' if namespace is None else 'session:' + namespace + ':'
    return prefix + hashlib.sha256(session.encode()).hexdigest()


def put(session, secret_id, namespace=None):
    key = _key(session, namespace)
    if key is None:
        raise ValueError('Invalid session')
    _, db = clients()
    db.put_item(TableName='team-context-sync', Item={'connection_id': {'S': key}, 'secret_id': {'S': secret_id}, 'expires_at': {'N': str(int(time.time()) + 2592000)}})


def get(session, namespace=None):
    key = _key(session, namespace)
    if key is None:
        return None
    _, db = clients()
    item = db.get_item(TableName='team-context-sync', Key={'connection_id': {'S': key}}, ConsistentRead=True).get('Item')
    if not item or int(item['expires_at']['N']) <= time.time():
        return None
    return item['secret_id']['S']


def remove(session, namespace=None):
    key = _key(session, namespace)
    if key is None:
        raise ValueError('Invalid session')
    _, db = clients()
    db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': key}})
