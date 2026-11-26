"""Exercise security mutations against the patched COA metric access paths."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

API = Path('packages/metric-service/src/coa_metrics/api/metric_api_handler.py')
MCP = Path('packages/mcp-server/src/coa_mcp/server.py')
STACK = Path('infra/lib/stacks/services/mcp-stack.ts')
MUTATIONS = {
    'allow_metric_api': (API, 'return api_response(403, {"message": "Metric access is disabled"})', 'return api_response(200, {"message": "Metric access is disabled"})'),
    'allow_non_get': (API, 'return api_response(403, {"message": "Metric access is disabled"})', 'return api_response(403 if event.get("httpMethod") == "GET" else 200, {"message": "Metric access is disabled"})'),
    'allow_metric_mcp': (MCP, '    raise ValueError("Metric access is disabled")', '    return "{}"'),
    'restore_metric_invoke': (STACK, '    // DynamoDB read (grant resolver needs roles + resource-role-mappings)', '    runtime.addToRolePolicy(new iam.PolicyStatement({ actions: ["lambda:InvokeFunction"], resources: ["*"] }));\n    // DynamoDB read (grant resolver needs roles + resource-role-mappings)'),
    'wildcard_lambda_action': (STACK, '    // DynamoDB read (grant resolver needs roles + resource-role-mappings)', '    runtime.addToRolePolicy(new iam.PolicyStatement({ actions: ["lambda:*"], resources: ["*"] }));\n    // DynamoDB read (grant resolver needs roles + resource-role-mappings)'),
}


def run(checkout):
    source = Path(checkout).resolve()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='coa-metric-lockdown-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        changes = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=changes, check=True)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(str(root / p) for p in ('packages/metric-service/src', 'packages/mcp-server/src', 'packages/context-manager/src'))
        pytest = ['uv', 'run', '--no-sync', '--with', 'mcp>=1.30,<2', '--with', 'pytest-asyncio>=0.23,<2', '--with', 'httpx>=0.27,<1', '--with', 'httpx-sse>=0.4,<1', 'pytest', '-q']
        commands = {
            API: (pytest + [str(root / 'packages/metric-service/tests/unit/test_metric_api_handler.py')], project),
            MCP: (pytest + [str(root / 'packages/mcp-server/tests/unit/test_server.py'), '-k', 'TestListMetricsTool'], project),
            STACK: ([str(source / 'infra/node_modules/.bin/jest'), 'test/services/mcp-stack.test.ts', '--runInBand', '--coverage=false'], root / 'infra'),
        }
        for command, cwd in commands.values():
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError('COA metric lockdown baseline failed:\n' + result.stdout + result.stderr)
        for name, (relative, old, new) in MUTATIONS.items():
            target = root / relative
            original = target.read_text()
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            command, cwd = commands[relative]
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            output = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout + result.stderr)
            pattern = r'Tests:\s+\d+ failed' if relative == STACK else r'(?m)^\d+ failed(?:,|\s)'
            if result.returncode != 1 or not re.search(pattern, output):
                raise RuntimeError(f'Mutation {name} was not caught by a test assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_metric_lockdown_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
