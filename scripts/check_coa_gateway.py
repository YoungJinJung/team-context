"""Inspect COA runtime gateway restrictions; this does not authorize private data."""
import argparse
import re

import boto3

_RUNTIME = re.compile(r'arn:(aws(?:-[a-z]+)*):bedrock-agentcore:([a-z0-9-]+):([0-9]{12}):runtime/([A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10})')
_GATEWAY_ID = re.compile(r'[A-Za-z0-9-]{1,100}')
_ENDPOINT = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,47}')
_VERSION = re.compile(r'[1-9][0-9]{0,4}')


def _endpoints(client, runtime_id, runtime_arn):
    endpoints, seen_tokens, token = {}, set(), None
    for _ in range(10):
        params = {'agentRuntimeId': runtime_id, 'maxResults': 100}
        if token is not None:
            params['nextToken'] = token
        page = client.list_agent_runtime_endpoints(**params)
        rows = page['runtimeEndpoints']
        if not isinstance(rows, list):
            raise ValueError('Invalid runtime endpoints')
        for row in rows:
            name = row['name']
            version = row.get('liveVersion')
            if not isinstance(name, str) or not _ENDPOINT.fullmatch(name) or name in endpoints or not isinstance(version, str) or not _VERSION.fullmatch(version):
                raise ValueError('Invalid runtime endpoint')
            if row.get('agentRuntimeArn') != runtime_arn or row.get('agentRuntimeEndpointArn') != runtime_arn + '/runtime-endpoint/' + name or row.get('status') != 'READY' or row.get('targetVersion', version) != version:
                raise ValueError('Unstable runtime endpoint')
            endpoints[name] = version
        if len(endpoints) > 100:
            raise ValueError('Too many runtime endpoints')
        token = page.get('nextToken')
        if token is None:
            if 'DEFAULT' not in endpoints:
                raise ValueError('Missing default endpoint')
            return endpoints
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise ValueError('Invalid pagination')
        seen_tokens.add(token)
    raise ValueError('Too many pages')


def check(client, runtime_arn, gateway_arn):
    """Check all currently visible runtime endpoint versions, not private-data safety."""
    try:
        match = _RUNTIME.fullmatch(runtime_arn) if isinstance(runtime_arn, str) else None
        if not match:
            raise ValueError('Invalid runtime')
        partition, region, account, runtime_id = match.groups()
        prefix = f'arn:{partition}:bedrock-agentcore:{region}:{account}:gateway/'
        if not isinstance(gateway_arn, str) or not gateway_arn.startswith(prefix) or not _GATEWAY_ID.fullmatch(gateway_arn[len(prefix):]) or client.meta.region_name != region:
            raise ValueError('Invalid gateway')
        endpoints = _endpoints(client, runtime_id, runtime_arn)
        for version in set(endpoints.values()):
            runtime = client.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=version)
            if runtime.get('agentRuntimeArn') != runtime_arn or runtime.get('agentRuntimeId') != runtime_id or runtime.get('agentRuntimeVersion') != version or runtime.get('status') != 'READY':
                raise ValueError('Invalid runtime version')
            authorizer = runtime['authorizerConfiguration']['customJWTAuthorizer']
            restriction = authorizer['allowedWorkloadConfiguration']
            if set(restriction) != {'hostingEnvironments'} or restriction['hostingEnvironments'] != [{'arn': gateway_arn}]:
                raise ValueError('Runtime is not gateway-only')
        if _endpoints(client, runtime_id, runtime_arn) != endpoints:
            raise ValueError('Runtime endpoints changed')
    except Exception:
        raise ValueError('Unable to verify gateway-only runtime configuration') from None
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-arn', required=True)
    parser.add_argument('--gateway-arn', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args()
    try:
        client = boto3.client('bedrock-agentcore-control', region_name=args.region)
        check(client, args.runtime_arn, args.gateway_arn)
    except Exception:
        parser.exit(1, 'Unable to verify gateway-only runtime configuration.\n')
    print('Gateway-only runtime configuration observed. This does not establish private-data safety.')


if __name__ == '__main__':
    main()
