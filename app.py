"""Local, read-only connector demo. Not a production server."""
import importlib
import json
import os
import http.cookies
import urllib.parse
import pkgutil
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import connectors
from storage import persist, search, remove_public_source, cleanup_unavailable, list_public_sources
from auth.sessions import Sessions
from auth.providers import PROVIDERS
from auth import linear_preview, teams, teams_preview, gitlab, gitlab_preview, gitlab_private, github, github_preview
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).parent
for provider in PROVIDERS.values():
    if provider.id == "linear":
        globals()[provider.session_name] = Sessions()
    else:
        module = importlib.import_module("auth." + provider.id.replace("-", "_"))
        globals()[provider.session_name] = Sessions(module.Authorization, module.refresh, provider.namespace)
STORAGE_LOCK = threading.Lock()
CALLBACK = "http://127.0.0.1:8765/oauth/linear/callback"
TEAMS_CALLBACK = "http://127.0.0.1:8765/oauth/teams/callback"


def discover():
    result = {}
    for item in pkgutil.iter_modules(connectors.__path__):
        module = importlib.import_module(f"connectors.{item.name}")
        manifest = module.MANIFEST
        if manifest["id"] in result:
            raise ValueError(f"Duplicate connector: {manifest['id']}")
        result[manifest["id"]] = module
    return result


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data, content_type="application/json; charset=utf-8"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code="-", size="-"):
        # Callback query strings contain authorization codes and must not reach logs.
        self.log_message('"%s %s" %s %s', self.command, urllib.parse.urlsplit(self.path).path, str(code), str(size))

    def cookie(self, name="team_context_session"):
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(name)
            return value.value if value else None
        except http.cookies.CookieError:
            return None

    def oauth_status(self, provider):
        if provider.id != "linear" and self.headers.get("Host") != "127.0.0.1:8765":
            return self.send(403, {"error": "Invalid host"})
        try:
            return self.send(200, {"connected": globals()[provider.session_name].connected(self.cookie(provider.cookie))})
        except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
            message = "Unable to read connection status" if provider.id == "linear" else f"Unable to load {provider.display_name} data"
            return self.send(502, {"error": message})

    def oauth_callback(self, provider, query):
        if self.headers.get("Host") != "127.0.0.1:8765":
            return self.send(403, {"error": "Invalid host"})
        values = urllib.parse.parse_qs(query)
        if len(values.get("state", [])) != 1 or len(values.get("code", [])) != 1:
            return self.send(400, {"error": "Invalid callback"})
        try:
            globals()[provider.session_name].complete(self.cookie(provider.cookie), values["state"][0], values["code"][0])
        except ValueError:
            return self.send(400, {"error": "Invalid or expired authorization"})
        except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
            return self.send(502, {"error": "Authorization failed"})
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def oauth_manage(self, provider):
        if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
            return self.send(403, {"error": "Invalid origin"})
        sessions = globals()[provider.session_name]
        session = self.cookie(provider.cookie)
        disconnect = self.path == f"/api/{provider.id}/disconnect"
        try:
            if disconnect:
                sessions.disconnect(session)
                return self.send(200, {"connected": False})
            if sessions.connected(session):
                return self.send(409, {"error": "Disconnect the existing connection first."})
            client_id = os.environ.get(provider.client_env)
            if not client_id or any(not os.environ.get(key) for key in provider.extra_env):
                return self.send(503, {"error": provider.missing_config})
            session, url = sessions.start(client_id, provider.callback)
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme, parsed.netloc) != ("https", urllib.parse.urlsplit(provider.authorize_origin).netloc):
                raise ValueError("Invalid authorization destination")
        except ValueError:
            return self.send(404, {"error": "No connection"})
        except (OSError, BotoCoreError, ClientError):
            if provider.id == "linear":
                message = "Disconnect failed" if disconnect else "Unable to read connection status"
            else:
                message = f"Unable to manage {provider.display_name} connection"
            return self.send(502, {"error": message})
        body = json.dumps({"url": url}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", provider.cookie + "=" + session + "; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        for provider in PROVIDERS.values():
            if parsed.path == f"/oauth/{provider.id}/callback":
                return self.oauth_callback(provider, parsed.query)
            if parsed.path == f"/api/{provider.id}/status" and (provider.id != "linear" or self.path == parsed.path):
                return self.oauth_status(provider)
        if parsed.path == "/api/gitlab/private/search":
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            params = urllib.parse.parse_qs(parsed.query)
            project = params.get("project", [""])[0]
            query = params.get("q", [""])[0]
            try:
                result = GITLAB_SESSIONS.use(self.cookie("team_context_gitlab_session"), lambda token: gitlab_private.search(token, project, query))
                return self.send(200, {"items": result})
            except ValueError:
                return self.send(400, {"error": "Private search unavailable"})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to search private data"})
        if parsed.path == "/api/github/preview":
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            session = self.cookie("team_context_github_session")
            try:
                repository = urllib.parse.parse_qs(parsed.query).get("repository", [""])[0]
                result = GITHUB_SESSIONS.use(session, lambda token: github_preview.pull_requests(token, repository))
                return self.send(200, {"items": result})
            except ValueError:
                return self.send(400, {"error": "GitHub preview unavailable"})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to load GitHub data"})
        if parsed.path == "/api/gitlab/preview":
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            session = self.cookie("team_context_gitlab_session")
            try:
                project = urllib.parse.parse_qs(parsed.query).get("project", [""])[0]
                result = GITLAB_SESSIONS.use(session, lambda token: gitlab_preview.merge_requests(token, project))
                return self.send(200, {"items": result})
            except ValueError:
                return self.send(400, {"error": "GitLab preview unavailable"})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to load GitLab data"})
        if parsed.path in ("/api/teams/list", "/api/teams/channels"):
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            session = self.cookie("team_context_teams_session")
            try:
                if parsed.path == "/api/teams/list":
                    result = TEAMS_SESSIONS.use(session, teams_preview.teams)
                else:
                    team = urllib.parse.parse_qs(parsed.query).get("team", [""])[0]
                    result = TEAMS_SESSIONS.use(session, lambda token: teams_preview.channels(token, team))
                return self.send(200, {"items": result})
            except ValueError:
                return self.send(400, {"error": "Teams preview unavailable"})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to load Teams data"})
        if parsed.path == "/api/sources":
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            try:
                return self.send(200, {"sources": list_public_sources()})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to list local sources"})
        if parsed.path == "/api/search":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            try:
                return self.send(200, {"records": search(query)})
            except ValueError as error:
                return self.send(400, {"error": str(error)})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to search local storage."})
        if parsed.path in ("/api/linear/teams", "/api/linear/issues"):
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            try:
                if parsed.path == "/api/linear/teams":
                    result = SESSIONS.use(self.cookie(), linear_preview.teams)
                else:
                    team = urllib.parse.parse_qs(parsed.query).get("team", [""])[0]
                    result = SESSIONS.use(self.cookie(), lambda token: linear_preview.issues(token, team))
                return self.send(200, {"items": result})
            except ValueError:
                return self.send(400, {"error": "Linear preview unavailable"})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to load Linear data"})
        if self.path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/connectors":
            return self.send(200, [m.MANIFEST for m in discover().values()])
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        for provider in PROVIDERS.values():
            if self.path in (f"/api/{provider.id}/start", f"/api/{provider.id}/disconnect"):
                return self.oauth_manage(provider)
        if self.path == "/api/gitlab/private/collect":
            if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
                return self.send(403, {"error": "Invalid origin"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or not isinstance(payload.get("project"), str):
                    raise ValueError("Invalid project")
                with STORAGE_LOCK:
                    count = GITLAB_SESSIONS.use(self.cookie("team_context_gitlab_session"), lambda token: gitlab_private.collect(token, payload["project"]))
                return self.send(200, {"count": count})
            except ValueError:
                return self.send(400, {"error": "Private collection unavailable"})
            except (OSError, BotoCoreError, ClientError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to collect private data"})
        if self.path == "/api/cleanup-unavailable":
            if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
                return self.send(403, {"error": "Invalid origin"})
            try:
                with STORAGE_LOCK:
                    count = cleanup_unavailable()
                return self.send(200, {"removed": count})
            except (OSError, BotoCoreError, ClientError, ValueError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to verify or clean local records"})
        if self.path not in ("/api/collect", "/api/remove-source"):
            return self.send(404, {"error": "Not found"})
        if self.headers.get("Origin") not in (None, "http://127.0.0.1:8765", "http://localhost:8765"):
            return self.send(403, {"error": "Invalid origin"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            module = discover().get(payload.get("connector"))
            if module is None or not isinstance(payload.get("config"), dict):
                raise ValueError("Invalid connector or configuration")
            with STORAGE_LOCK:
                if self.path == "/api/remove-source":
                    if payload["connector"] not in ("github", "gitlab"):
                        raise ValueError("Unsupported source")
                    count = remove_public_source(module.connection_id(payload["config"]))
                    self.send(200, {"removed": count})
                else:
                    records = module.collect(payload["config"])
                    persist(module.connection_id(payload["config"]), records)
                    self.send(200, {"records": records})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except (urllib.error.URLError, TimeoutError, OSError, BotoCoreError, ClientError):
            self.send(502, {"error": "Unable to fetch or store data. Check the source service and local AWS emulator."})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Open http://127.0.0.1:8765", flush=True)
    server.serve_forever()
