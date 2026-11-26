"""Test security mutations against patched COA schema discovery paths."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DATA = Path('packages/data-layer/src/coa_data_layer/handler.py')
MCP = Path('packages/mcp-server/src/coa_mcp/server.py')
DATA_STACK = Path('infra/lib/stacks/services/data-layer-stack.ts')
MCP_STACK = Path('infra/lib/stacks/services/mcp-stack.ts')
MUTATIONS = {
    'allow_rest_schema': (DATA, 'return _error_response(403, "Schema discovery is disabled")', 'return _error_response(200, "Schema discovery is disabled")'),
    'redirect_rest_schema': (DATA, '("GET", "/namespaces/{namespaceId}/schema"): _handle_describe_schema', '("GET", "/namespaces/{namespaceId}/schema"): _handle_query'),
    'allow_mcp_schema': (MCP, '    raise ValueError("Schema discovery is disabled")', '    return "{}"'),
    'restore_data_invoke': (DATA_STACK, '    // ── Publish Lambda ARN to SSM', '    this.apiFn.addToRolePolicy(new (require("aws-cdk-lib/aws-iam").PolicyStatement)({ actions: ["lambda:InvokeFunction"], resources: ["*"] }));\n    // ── Publish Lambda ARN to SSM'),
    'restore_mcp_invoke': (MCP_STACK, 'resources: [metricServiceLambdaArn]', 'resources: [metricServiceLambdaArn, "*"]'),
    'wildcard_lambda_action': (MCP_STACK, 'actions: ["lambda:InvokeFunction"]', 'actions: ["lambda:*"]'),
}


def run(checkout):
    source = Path(checkout).resolve()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='coa-schema-lockdown-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        changes = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=changes, check=True)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(str(root / p) for p in ('packages/data-layer/src', 'packages/mcp-server/src', 'packages/context-manager/src'))
        python_base = ['uv', 'run', '--no-sync', '--with', 'mcp>=1.30,<2', '--with', 'pytest-asyncio>=0.23,<2', '--with', 'httpx>=0.27,<1', '--with', 'httpx-sse>=0.4,<1', 'pytest', '-q']
        commands = {
            DATA: (python_base + [str(root / 'packages/data-layer/tests/unit/test_handler_validation.py'), '-k', 'TestDescribeSchema'], project),
            MCP: (python_base + [str(root / 'packages/mcp-server/tests/unit/test_server.py'), '-k', 'TestDescribeSchemaTool'], project),
            DATA_STACK: ([str(source / 'infra/node_modules/.bin/jest'), 'test/services/data-layer-stack.test.ts', '--runInBand', '--coverage=false'], root / 'infra'),
            MCP_STACK: ([str(source / 'infra/node_modules/.bin/jest'), 'test/services/mcp-stack.test.ts', '--runInBand', '--coverage=false'], root / 'infra'),
        }
        for command, cwd in commands.values():
            baseline = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            if baseline.returncode:
                raise RuntimeError('Schema lockdown baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, (relative, old, new) in MUTATIONS.items():
            target = root / relative
            original = target.read_text()
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            command, cwd = commands[relative]
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            output = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout + result.stderr)
            pattern = r'Tests:\s+\d+ failed' if relative in (DATA_STACK, MCP_STACK) else r'(?m)^\d+ failed(?:,|\s)'
            if result.returncode != 1 or not re.search(pattern, output):
                raise RuntimeError(f'Mutation {name} was not caught by a test assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_schema_lockdown_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
