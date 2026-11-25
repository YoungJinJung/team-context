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
from storage import persist, search, remove_public_source, cleanup_unavailable
from auth.sessions import Sessions
from auth import linear_preview
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).parent
SESSIONS = Sessions()
STORAGE_LOCK = threading.Lock()
CALLBACK = "http://127.0.0.1:8765/oauth/linear/callback"


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

    def cookie(self):
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("team_context_session")
            return value.value if value else None
        except http.cookies.CookieError:
            return None

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/oauth/linear/callback":
            if self.headers.get("Host") != "127.0.0.1:8765":
                return self.send(403, {"error": "Invalid host"})
            values = urllib.parse.parse_qs(parsed.query)
            if len(values.get("state", [])) != 1 or len(values.get("code", [])) != 1:
                return self.send(400, {"error": "Invalid callback"})
            try:
                SESSIONS.complete(self.cookie(), values["state"][0], values["code"][0])
            except ValueError:
                return self.send(400, {"error": "Invalid or expired authorization"})
            except (OSError, BotoCoreError, ClientError):
                return self.send(502, {"error": "Authorization failed"})
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
        if self.path == "/api/linear/status":
            try:
                return self.send(200, {"connected": SESSIONS.connected(self.cookie())})
            except (OSError, BotoCoreError, ClientError):
                return self.send(502, {"error": "Unable to read connection status"})
        if self.path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/connectors":
            return self.send(200, [m.MANIFEST for m in discover().values()])
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/cleanup-unavailable":
            if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
                return self.send(403, {"error": "Invalid origin"})
            try:
                with STORAGE_LOCK:
                    count = cleanup_unavailable()
                return self.send(200, {"removed": count})
            except (OSError, BotoCoreError, ClientError, ValueError, KeyError, TypeError):
                return self.send(502, {"error": "Unable to verify or clean local records"})
        if self.path == "/api/linear/disconnect":
            if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
                return self.send(403, {"error": "Invalid origin"})
            try:
                SESSIONS.disconnect(self.cookie())
            except ValueError:
                return self.send(404, {"error": "No connection"})
            except (OSError, BotoCoreError, ClientError):
                return self.send(502, {"error": "Disconnect failed"})
            return self.send(200, {"connected": False})
        if self.path == "/api/linear/start":
            if self.headers.get("Host") != "127.0.0.1:8765" or self.headers.get("Origin") != "http://127.0.0.1:8765":
                return self.send(403, {"error": "Invalid origin"})
            try:
                if SESSIONS.connected(self.cookie()):
                    return self.send(409, {"error": "Disconnect the existing connection first."})
            except (OSError, BotoCoreError, ClientError):
                return self.send(502, {"error": "Unable to read connection status"})
            client_id = os.environ.get("LINEAR_CLIENT_ID")
            if not client_id:
                return self.send(503, {"error": "Set LINEAR_CLIENT_ID to your Linear OAuth app client ID."})
            session, url = SESSIONS.start(client_id, CALLBACK)
            body = json.dumps({"url": url}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", "team_context_session=" + session + "; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
