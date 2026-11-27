"""Exercise subject-boundary mutations against the pinned COA runtime."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAIN = Path('packages/context-manager/src/coa_serve/main.py')
TEST = Path('packages/context-manager/tests/unit/test_jwt_subject_gate.py')
MUTATIONS = {
    'skip_subject_check': ('if (not isinstance(_jwt_user_id, str) or not _jwt_user_id.strip() or len(_jwt_user_id) > 255\n            or any(ord(char) < 32 or ord(char) == 127 for char in _jwt_user_id)):', 'if False:'),
    'allow_blank_subject': ('not _jwt_user_id.strip()', 'not _jwt_user_id'),
    'allow_long_subject': ('len(_jwt_user_id) > 255', 'len(_jwt_user_id) > 999'),
    'allow_control_characters': ('or any(ord(char) < 32 or ord(char) == 127 for char in _jwt_user_id)', 'or False'),
    'coerce_subject_type': ('    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)\n', '    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)\n    _jwt_user_id = str(_jwt_user_id)\n'),
    'trust_body_identity': ('    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)\n', '    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)\n    if not _jwt_user_id:\n        _jwt_user_id = (payload.get("profile") or {}).get("userId", "")\n'),
    'initialize_before_auth': ('    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)\n', '    await _ensure_initialized()\n    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)\n'),
    'break_stream_error': ('yield SSEEmitter(request_id).format_error("AccessDenied", "Authentication required", 403)', 'yield AccessDeniedError("Authentication required").to_dict(request_id)'),
    'deny_valid_subject': ('if (not isinstance(_jwt_user_id, str)', 'if (True or not isinstance(_jwt_user_id, str)'),
}


def run(checkout):
    source = Path(checkout).resolve()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='coa-jwt-subject-mutation-') as temp:
        root = Path(temp) / 'coa'
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(source), str(root)], check=True)
        changes = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=source, capture_output=True, check=True).stdout
        subprocess.run(['git', 'apply', '-'], cwd=root, input=changes, check=True)
        if not (root / TEST).exists():
            (root / TEST).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / TEST, root / TEST)
        env = os.environ.copy()
        env['PYTHONPATH'] = str(root / 'packages/context-manager/src')
        command = ['uv', 'run', '--no-sync', '--with', 'pytest-asyncio>=0.23,<2', '--with', 'regex==2026.5.9', '--with', 'bedrock-agentcore==1.18.1', '--with', 'httpx>=0.27,<1', '--with', 'opensearch-py>=2.8,<4', '--with', 'requests-aws4auth>=1.3,<2', '--with', 'sqlglot>=25,<30', '--with', 'cedarpy>=4.8,<5', '--with', 'python-ulid>=3,<4', '--with', 'rdflib>=7,<8', '--with', 'sqlalchemy[asyncio]>=2,<3', '--with', 'asyncpg>=0.30,<1', '--with', 'aiomysql>=0.2,<1', '--with', 'python-tds>=1.15,<2', '--with', 'strands-agents>=1.48,<2', '--with', 'retryhttp>=1.4,<2', 'pytest', '-q', str(root / TEST)]
        baseline = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True)
        if baseline.returncode:
            raise RuntimeError('COA JWT subject baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, (old, new) in MUTATIONS.items():
            target = root / MAIN
            original = target.read_text()
            if original.count(old) != 1:
                raise RuntimeError(f'Cannot apply mutation {name}')
            target.write_text(original.replace(old, new))
            result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True)
            output = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout + result.stderr)
            if result.returncode != 1 or not re.search(r'(?m)^\d+ failed(?:,|\s)', output) or 'AssertionError' not in output:
                raise RuntimeError(f'Mutation {name} was not caught by an assertion:\n{output}')
            print(f'Caught {name}', flush=True)
            target.write_text(original)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tests/coa_jwt_subject_mutations.py PATCHED_COA_CHECKOUT')
    run(sys.argv[1])
