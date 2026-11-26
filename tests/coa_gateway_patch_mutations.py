"""Run security-focused mutations against a patched, disposable COA checkout."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path


MUTATIONS = {
    'missing_gateway_fails_open': (
        'const gatewayArn = this.node.tryGetContext("team_context_gateway_arn");',
        'const gatewayArn = this.node.tryGetContext("team_context_gateway_arn") || '
        '"arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/tc-0123456789";',
    ),
    'allow_wrong_region': ('gatewayMatch[2] !== region || ', ''),
    'allow_wrong_account': (' || gatewayMatch[3] !== account', ''),
    'omit_workload_restriction': (
        'AuthorizerConfiguration.CustomJWTAuthorizer.AllowedWorkloadConfiguration',
        'AuthorizerConfiguration.CustomJWTAuthorizer.UnrelatedConfiguration',
    ),
    'allow_extra_identity': (
        '{ HostingEnvironments: [{ Arn: gatewayArn }] }',
        '{ HostingEnvironments: [{ Arn: gatewayArn }], WorkloadIdentities: ["extra"] }',
    ),
}


def run(checkout):
    source = Path(checkout).resolve()
    with tempfile.TemporaryDirectory(prefix='coa-gateway-mutation-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        patch = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=patch, check=True)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        target = root / 'infra/lib/stacks/services/serve-stack.ts'
        original = target.read_text()
        cmd = [str(source / 'infra/node_modules/.bin/jest'), 'test/services/serve-stack.test.ts',
               '--runInBand', '--coverage=false', '--testNamePattern=ServeStack - gateway-only runtime']
        baseline = subprocess.run(cmd, cwd=root / 'infra', capture_output=True, text=True)
        if baseline.returncode != 0:
            raise RuntimeError('COA gateway baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, (old, new) in MUTATIONS.items():
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            result = subprocess.run(cmd, cwd=root / 'infra', capture_output=True, text=True)
            output = result.stdout + result.stderr
            if result.returncode == 0 or not re.search(r'Tests:\s+\d+ failed', output):
                raise RuntimeError(f'Mutation {name} was not caught by a test assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_gateway_patch_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
