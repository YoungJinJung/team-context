"""Read pull requests from a public GitHub repository."""
import json
import re
import urllib.request
from auth.transport import open_url

MANIFEST = {"id": "github", "name": "GitHub", "category": "source_code", "fields": [{"name": "repository", "label": "Repository (owner/name)", "required": True}]}


def connection_id(config):
    repository = config.get("repository", "")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or ".." in repository:
        raise ValueError("Repository must be in owner/name format")
    return "github:" + repository.lower()


def collect(config):
    connection_id(config)
    repository = config["repository"]
    pulls = []
    seen = set()
    for page in range(1, 101):
        url = f"https://api.github.com/repos/{repository}/pulls?state=all&per_page=100&page={page}"
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "team-context-mvp"})
        with open_url(request) as response:
            batch = json.load(response)
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError("Invalid GitHub page")
        for pull in batch:
            number = pull['number']
            if number in seen:
                raise ValueError("Repeated GitHub pull request")
            seen.add(number)
        pulls.extend(batch)
        if len(batch) < 100:
            return [{"id": f"github:{repository.lower()}:pr:{p['number']}", "visibility": "public", "type": "pull_request", "title": p["title"], "url": p["html_url"], "updated_at": p["updated_at"], "body": p.get("body") or ""} for p in pulls]
    raise ValueError("GitHub collection exceeds 100 pages")
