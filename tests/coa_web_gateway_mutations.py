"""Exercise security mutations against a patched COA web application."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ENDPOINT = Path('packages/web-app/src/utils/build-query-endpoint.ts')
CLIENT = Path('packages/web-app/src/utils/invoke-agentcore.ts')
STREAM = Path('packages/web-app/src/api-hooks/use-playground-stream.ts')
STACK = Path('infra/lib/stacks/foundation/web-stack.ts')
CSP = Path('infra/lib/constructs/public-ui-construct.ts')
MUTATIONS = {
    'direct_runtime_url': (ENDPOINT, 'https://${arn[4]}.gateway.bedrock-agentcore.${ctx.region}.${suffix}/${ctx.serveGatewayTarget}/invocations', 'https://bedrock-agentcore.${ctx.region}.amazonaws.com/runtimes/direct/invocations'),
    'wrong_account': (ENDPOINT, ' || arn[3] !== ctx.serveGatewayAccount', ''),
    'wrong_region': (ENDPOINT, ' || arn[2] !== ctx.region', ''),
    'unsafe_target': (ENDPOINT, ' || !target || target[0] !== ctx.serveGatewayTarget', ''),
    'client_redirect': (CLIENT, '        redirect: "error",\n', ''),
    'stream_redirect': (STREAM, '          redirect: "error",\n', ''),
    'stack_wrong_account': (STACK, ' || gatewayMatch[3] !== this.account', ''),
    'stack_wrong_region': (STACK, 'gatewayMatch[2] !== this.region || ', ''),
    'stack_custom_csp': (STACK, ' || props.contentSecurityPolicy', ''),
    'csp_wrong_origin': (CSP, 'https://${gateway[4]}.gateway.bedrock-agentcore.', 'https://direct.gateway.bedrock-agentcore.'),
}


def run(checkout):
    source = Path(checkout).resolve()
    with tempfile.TemporaryDirectory(prefix='coa-web-gateway-mutation-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        patch = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=patch, check=True)
        for relative in (Path('packages/web-app/tests/unit/utils/build-query-endpoint.test.ts'), Path('packages/web-app/tests/unit/utils/gateway-requests.test.tsx')):
            if not (root / relative).exists():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, root / relative)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared'), Path('packages/web-app')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        vitest = [str(source / 'packages/web-app/node_modules/.bin/vitest'), 'run',
                  'tests/unit/utils/build-query-endpoint.test.ts', 'tests/unit/utils/gateway-requests.test.tsx']
        jest = [str(source / 'infra/node_modules/.bin/jest'), 'test/foundation/web-stack.test.ts',
                'test/constructs/public-ui-csp.test.ts', '--runInBand', '--coverage=false',
                '--testNamePattern=gateway routing|configured gateway']
        env = os.environ.copy()
        for cmd, cwd in ((vitest, root / 'packages/web-app'), (jest, root / 'infra')):
            baseline = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
            if baseline.returncode:
                raise RuntimeError('COA web gateway baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, (relative, old, new) in MUTATIONS.items():
            target = root / relative
            original = target.read_text()
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            is_infra = relative in (STACK, CSP)
            result = subprocess.run(jest if is_infra else vitest, cwd=root / ('infra' if is_infra else 'packages/web-app'), env=env, capture_output=True, text=True)
            output = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout + result.stderr)
            failed = re.search(r'Tests:\s+\d+ failed' if is_infra else r'Tests\s+\d+ failed', output)
            if result.returncode != 1 or not failed:
                raise RuntimeError(f'Mutation {name} was not caught by a test assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_web_gateway_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
