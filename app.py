"""Local, read-only connector demo. Not a production server."""
import importlib
import json
import pkgutil
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import connectors
from storage import persist
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).parent


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
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/connectors":
            return self.send(200, [m.MANIFEST for m in discover().values()])
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/collect":
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
            records = module.collect(payload["config"])
            persist(payload["connector"], payload["config"], records)
            self.send(200, {"records": records})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except (urllib.error.URLError, TimeoutError, OSError, BotoCoreError, ClientError):
            self.send(502, {"error": "Unable to fetch or store data. Check GitHub and the local AWS emulator."})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Open http://127.0.0.1:8765", flush=True)
    server.serve_forever()
