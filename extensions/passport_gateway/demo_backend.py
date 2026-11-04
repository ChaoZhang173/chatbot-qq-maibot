"""Loopback-only protocol example. Echoes text; no model, tools, memory or speech API calls."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backend import MAX_PACKET
from .config import DEVICE_ID_RE


def make_server(api_key: str, devices: set[str], port: int = 8766) -> ThreadingHTTPServer:
    if not api_key or any(c.isspace() for c in api_key):
        raise ValueError("PASSPORT_BACKEND_API_KEY is required and must not contain whitespace")
    if not devices or any(not DEVICE_ID_RE.fullmatch(d) for d in devices):
        raise ValueError("At least one valid demo device ID is required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log headers, request bodies or conversation text.

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/v1/voice":
                self.send_json(404, {"ok": False, "error": "not_found"})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + api_key):
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            if self.headers.get("X-Passport-Protocol") != "1":
                self.send_json(400, {"ok": False, "error": "unsupported_protocol"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_PACKET:
                    raise ValueError("size")
                data = json.loads(self.rfile.read(length))
                if (not isinstance(data, dict)
                        or set(data) != {"op", "device_id", "turn_uid", "text"}
                        or data["op"] != "voice_turn"
                        or not isinstance(data["device_id"], str)
                        or not isinstance(data["turn_uid"], str)
                        or not re.fullmatch(r"[0-9a-f]{32}", data["turn_uid"])
                        or not isinstance(data["text"], str) or not data["text"].strip()
                        or len(data["text"]) > 4096):
                    raise ValueError("request")
            except (ValueError, TypeError, OSError):
                self.send_json(400, {"ok": False, "error": "invalid_request"})
                return
            if data["device_id"] not in devices:
                self.send_json(200, {"ok": False, "error": "voice_unbound"})
                return
            # Replace this pure echo with your backend's user/session lookup and chat call.
            # Real backends must persist turn deduplication before performing side effects.
            self.send_json(200, {"ok": True, "reply": "Demo echo: " + data["text"],
                                 "mood": "neutral", "memory_stored": False})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    key = os.environ.get("PASSPORT_BACKEND_API_KEY", "")
    devices = {d.strip() for d in os.environ.get("PASSPORT_DEMO_DEVICE_IDS", "demo-device").split(",") if d.strip()}
    try:
        server = make_server(key, devices, args.port)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(f"Demo echo backend: http://127.0.0.1:{server.server_port}/v1/voice (no chatbot model)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
