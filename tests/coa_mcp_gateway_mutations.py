"""Exercise security mutations against a patched, disposable COA checkout."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


CLIENT = Path('packages/mcp-server/src/coa_mcp/clients/context_manager.py')
SERVER = Path('packages/mcp-server/src/coa_mcp/server.py')
STACK = Path('infra/lib/stacks/services/mcp-stack.ts')
MUTATIONS = {
    'direct_runtime_fallback': (CLIENT,
        'self._url = f"https://{arn.group(4)}.gateway.bedrock-agentcore.{region}.{suffix}/{target}/invocations"',
        'self._url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/fake/invocations"'),
    'wrong_account': (CLIENT, ' or arn.group(3) != account', ''),
    'wrong_region': (CLIENT, ' or arn.group(2) != region', ''),
    'unsafe_target': (CLIENT, ' or not valid_target', ''),
    'follow_redirects': (CLIENT, 'follow_redirects=False,', 'follow_redirects=True,'),
    'replace_caller_token': (CLIENT, '"Authorization": f"Bearer {bearer_token}"', '"Authorization": "Bearer wrong"'),
    'shared_session': (CLIENT, 'headers[RUNTIME_SESSION_HEADER] = runtime_session_id',
        'headers[RUNTIME_SESSION_HEADER] = "shared-session"'),
    'missing_gateway_fallback': (SERVER, 'os.environ.get("CM_GATEWAY_ARN", "")',
        'os.environ.get("CM_GATEWAY_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/tc-0123456789")'),
    'stack_wrong_account': (STACK, ' || gatewayMatch[3] !== account', ''),
    'stack_wrong_region': (STACK, 'gatewayMatch[2] !== region || ', ''),
    'direct_runtime_iam': (STACK, '    // SSM read (resolve CM runtime ARN, other config)',
        '    runtime.addToRolePolicy(new iam.PolicyStatement({ actions: ["bedrock-agentcore:InvokeAgentRuntime"], resources: ["*"] }));\n'
        '    // SSM read (resolve CM runtime ARN, other config)'),
}


def run(checkout):
    source = Path(checkout).resolve()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='coa-mcp-gateway-mutation-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        patch = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=patch, check=True)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(str(root / p) for p in ('packages/mcp-server/src', 'packages/context-manager/src'))
        unit = root / 'packages/mcp-server/tests/unit'
        python_cmd = ['uv', 'run', '--no-sync', '--with', 'mcp>=1.30,<2', '--with', 'pytest-asyncio',
                      '--with', 'httpx', '--with', 'httpx-sse', 'pytest', '-q', str(unit / 'test_context_manager.py'),
                      str(unit / 'test_context_manager_timeout.py'), str(unit / 'test_server.py')]
        jest_cmd = [str(source / 'infra/node_modules/.bin/jest'), 'test/services/mcp-stack.test.ts',
                    '--runInBand', '--coverage=false']
        for command, cwd in ((python_cmd, project), (jest_cmd, root / 'infra')):
            baseline = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            if baseline.returncode != 0:
                raise RuntimeError('COA MCP gateway baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, (relative, old, new) in MUTATIONS.items():
            target = root / relative
            original = target.read_text()
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            is_stack = relative == STACK
            result = subprocess.run(jest_cmd if is_stack else python_cmd,
                cwd=root / 'infra' if is_stack else project, env=env, capture_output=True, text=True)
            output = result.stdout + result.stderr
            failed_assertion = re.search(r'Tests:\s+\d+ failed' if is_stack else r'(?m)^\d+ failed(?:,|\s)', output)
            if result.returncode != 1 or not failed_assertion:
                raise RuntimeError(f'Mutation {name} was not caught by a test assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_mcp_gateway_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
