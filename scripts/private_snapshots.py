"""Inspect or remove local GitLab private snapshots as a local operator."""
import argparse
import re
from auth.gitlab_private import _key
from storage import clients


def project_id(value):
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise argparse.ArgumentTypeError('Project ID must be a positive integer')
    return int(value)


def list_ids():
    s3, _ = clients()
    ids = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket='team-context-records', Prefix='private/gitlab/'):
        for item in page.get('Contents', []):
            match = re.fullmatch(r'private/gitlab/([1-9][0-9]{0,18})\.json', item['Key'])
            if match:
                ids.append(int(match.group(1)))
    return sorted(ids)


def remove(identifier):
    key = _key(identifier)
    s3, _ = clients()
    s3.delete_object(Bucket='team-context-records', Key=key)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage local GitLab private snapshots.')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('list', help='List local GitLab project IDs.')
    delete = commands.add_parser('remove', help='Remove a local GitLab snapshot.')
    delete.add_argument('project_id', type=project_id)
    delete.add_argument('--confirm', action='store_true', required=True, help='Confirm removal of this local snapshot.')
    args = parser.parse_args(argv)
    if args.action == 'list':
        for identifier in list_ids():
            print(identifier)
    else:
        remove(args.project_id)
        print('Local snapshot removal requested.')


if __name__ == '__main__':
    main()
