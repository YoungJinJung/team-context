"""Inspect or remove local GitHub and GitLab private snapshots as a local operator."""
import argparse
import re
from auth.gitlab_private import _key as gitlab_key
from auth.github_private import _key as github_key
from storage import clients


def project_id(value):
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise argparse.ArgumentTypeError('Project ID must be a positive integer')
    return int(value)


def _provider(value):
    if value not in ('gitlab', 'github'):
        raise ValueError('Invalid provider')
    return value


def list_ids(provider='gitlab'):
    provider = _provider(provider)
    s3, _ = clients()
    ids = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket='team-context-records', Prefix=f'private/{provider}/'):
        for item in page.get('Contents', []):
            match = re.fullmatch(r'private/' + provider + r'/([1-9][0-9]{0,18})\.json', item['Key'])
            if match:
                ids.append(int(match.group(1)))
    return sorted(ids)


def remove(identifier, provider='gitlab'):
    provider = _provider(provider)
    key = (gitlab_key if provider == 'gitlab' else github_key)(identifier)
    s3, _ = clients()
    s3.delete_object(Bucket='team-context-records', Key=key)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage local GitHub and GitLab private snapshots.')
    commands = parser.add_subparsers(dest='action', required=True)
    listing = commands.add_parser('list', help='List local project or repository IDs.')
    listing.add_argument('--provider', choices=('gitlab', 'github'), default='gitlab')
    delete = commands.add_parser('remove', help='Remove a local private snapshot.')
    delete.add_argument('project_id', type=project_id)
    delete.add_argument('--provider', choices=('gitlab', 'github'), default='gitlab')
    delete.add_argument('--confirm', action='store_true', required=True, help='Confirm removal of this local snapshot.')
    args = parser.parse_args(argv)
    if args.action == 'list':
        for identifier in list_ids(args.provider):
            print(identifier)
    else:
        remove(args.project_id, args.provider)
        print('Local snapshot removal requested.')


if __name__ == '__main__':
    main()
