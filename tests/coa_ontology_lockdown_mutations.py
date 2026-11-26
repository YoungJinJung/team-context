"""Exercise security mutations against the deployed COA ontology proxy."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STACK = Path('infra/lib/stacks/services/ontology-stack.ts')
MUTATIONS = {
    'allow_proxy': ('"statusCode": 403', '"statusCode": 200'),
    'change_proxy_response': ('Ontology API is disabled', 'Ontology API exposed'),
    'restore_original_handler': ('handler: "index.handler"', 'handler: "coa_ontology.api_proxy_handler.handler"'),
    'restore_vpc': ('      timeout: cdk.Duration.seconds(5),\n    });', '      timeout: cdk.Duration.seconds(5),\n      vpc,\n    });'),
    'add_environment': ('      timeout: cdk.Duration.seconds(5),\n    });', '      timeout: cdk.Duration.seconds(5),\n      environment: { ONTOLOGY_ENGINE_ENDPOINT: "http://engine" },\n    });'),
    'add_proxy_access': ('    new ssm.StringParameter(this, "SsmOntologyApiFnArn"', '    apiProxyFn.addToRolePolicy(new iam.PolicyStatement({ actions: ["neptune-db:ReadDataViaQuery"], resources: ["*"] }));\n    new ssm.StringParameter(this, "SsmOntologyApiFnArn"'),
}


def run(checkout):
    source = Path(checkout).resolve()
    with tempfile.TemporaryDirectory(prefix='coa-ontology-lockdown-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        changes = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=changes, check=True)
        for relative in (Path('.'), Path('infra'), Path('libs/ts-shared')):
            modules = source / relative / 'node_modules'
            if modules.exists():
                (root / relative / 'node_modules').symlink_to(modules, target_is_directory=True)
        command = [str(source / 'infra/node_modules/.bin/jest'), 'test/services/ontology-stack.test.ts', '--runInBand', '--coverage=false', '--testNamePattern=ontology proxy lockdown']
        baseline = subprocess.run(command, cwd=root / 'infra', capture_output=True, text=True)
        if baseline.returncode:
            raise RuntimeError('COA ontology lockdown baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, (old, new) in MUTATIONS.items():
            target = root / STACK
            original = target.read_text()
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            result = subprocess.run(command, cwd=root / 'infra', capture_output=True, text=True)
            output = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout + result.stderr)
            if result.returncode != 1 or not re.search(r'Tests:\s+\d+ failed', output):
                raise RuntimeError(f'Mutation {name} was not caught by a test assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_ontology_lockdown_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
