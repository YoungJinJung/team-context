"""Exercise security mutations against a patched, disposable COA checkout."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


HANDLER = Path('packages/data-layer/src/coa_data_layer/handler.py')
STACK = Path('infra/lib/stacks/services/data-layer-stack.ts')
MUTATIONS = {
    'direct_runtime_fallback': (HANDLER,
        'return f"https://{arn.group(4)}.gateway.bedrock-agentcore.{region}.{suffix}/{target}/invocations"',
        'return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/fake/invocations"'),
    'wrong_account': (HANDLER, ' or arn.group(3) != account', ''),
    'wrong_region': (HANDLER, ' or arn.group(2) != region', ''),
    'unsafe_target': (HANDLER, ' or not valid_target', ''),
    'follow_redirects': (HANDLER, 'urllib_request.build_opener(_NoRedirect()).open', 'urllib_request.build_opener().open'),
    'replace_caller_token': (HANDLER, '"Authorization": f"Bearer {token}"', '"Authorization": "Bearer wrong"'),
    'missing_gateway_fallback': (HANDLER, 'os.environ["CM_GATEWAY_ARN"]',
        'os.environ.get("CM_GATEWAY_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/tc-0123456789")'),
    'stack_wrong_account': (STACK, ' || gatewayMatch[3] !== account', ''),
    'stack_wrong_region': (STACK, 'gatewayMatch[2] !== region || ', ''),
    'direct_runtime_iam': (STACK, '    // ── IAM: invoke the discovery Lambda direct-through (bypass CM) ──',
        '    this.apiFn.addToRolePolicy(new iam.PolicyStatement({ actions: ["bedrock-agentcore:InvokeAgentRuntime"], resources: ["*"] }));\n'
        '    // ── IAM: invoke the discovery Lambda direct-through (bypass CM) ──'),
}


def run(checkout):
    source = Path(checkout).resolve()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='coa-data-gateway-mutation-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        patch = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=patch, check=True)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(str(root / p) for p in ('packages/data-layer/src', 'packages/context-manager/src'))
        unit = root / 'packages/data-layer/tests/unit'
        python_cmd = ['uv', 'run', '--no-sync', '--with', 'pytest-asyncio>=0.23,<2', 'pytest', '-q',
                      str(unit / 'test_handler_validation.py'), str(unit / 'test_rest_deadline_ordering.py')]
        jest_cmd = [str(source / 'infra/node_modules/.bin/jest'), 'test/services/data-layer-stack.test.ts',
                    '--runInBand', '--coverage=false']
        for command, cwd in ((python_cmd, project), (jest_cmd, root / 'infra')):
            baseline = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            if baseline.returncode != 0:
                raise RuntimeError('COA data-layer gateway baseline failed:\n' + baseline.stdout + baseline.stderr)
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
        raise SystemExit('Usage: python tests/coa_data_gateway_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
