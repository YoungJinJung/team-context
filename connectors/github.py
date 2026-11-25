"""Read pull requests from a public GitHub repository."""
import json
import re
import urllib.request

MANIFEST = {"id": "github", "name": "GitHub", "category": "source_code", "fields": [{"name": "repository", "label": "Repository (owner/name)", "required": True}]}


def collect(config):
    repository = config.get("repository", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or ".." in repository:
        raise ValueError("Repository must be in owner/name format")
    url = f"https://api.github.com/repos/{repository}/pulls?state=all&per_page=30"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "team-context-mvp"})
    with urllib.request.urlopen(request, timeout=15) as response:
        pulls = json.load(response)
    return [{"id": f"github:{repository.lower()}:pr:{p['number']}", "type": "pull_request", "title": p["title"], "url": p["html_url"], "updated_at": p["updated_at"], "body": p.get("body") or ""} for p in pulls]
