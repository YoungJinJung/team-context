"""Revalidate one source item for a verified caller before trusting a snapshot.

The caller must obtain the complete provenance from a trusted index. This
primitive is not a COA retrieval gate and does not enforce data-store isolation.
"""
import hashlib
import hmac
import json
import re

from auth import github_private, gitlab_private


def content_digest(record):
    """Hash the normalized source record used by the snapshot collector."""
    data = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


class SourceProvenance:
    """Fail closed when the caller cannot read the unchanged source item."""

    def __init__(self, credentials):
        self._credentials = credentials

    def allows(self, authorization, provenance):
        """Check one trusted provenance entry without returning its content."""
        try:
            if not isinstance(provenance, dict) or set(provenance) != {'provider', 'project', 'scope_id', 'number', 'digest'}:
                return False
            provider, project = provenance['provider'], provenance['project']
            scope_id, number, digest = provenance['scope_id'], provenance['number'], provenance['digest']
            if provider not in ('github', 'gitlab') or not isinstance(project, str) or type(scope_id) is not int or scope_id <= 0 or type(number) is not int or number <= 0 or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                return False
            token = self._credentials.resolve(authorization, provider).access_token
            if provider == 'github':
                current_scope, name = github_private._repository(token, project)
                if current_scope != scope_id:
                    return False
                item = github_private._get(token, f'repos/{name}/pulls/{number}')
                record = github_private._record(item, current_scope, name)
                if record['number'] != number:
                    return False
            else:
                current_scope, name = gitlab_private._project(token, project)
                if current_scope != scope_id:
                    return False
                item = gitlab_private._get(token, f'projects/{current_scope}/merge_requests/{number}')
                record = gitlab_private._record(item, current_scope, name)
                if record['iid'] != number:
                    return False
            return hmac.compare_digest(content_digest(record), digest)
        except Exception:
            return False
