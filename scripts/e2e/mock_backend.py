"""A tiny local mock of an extension's backend, for end-to-end tests.

  python mock_backend.py [--port 8100] [--host 127.0.0.1] [--state-file FILE]

Endpoints (JSON):
  POST /api/login     {"username": "demo", "password": "demo-pass"} -> {"data": {"access_token": ...}}
  POST /api/results   any JSON with "items": [...], needs "Authorization: Bearer <token>"
  GET  /runner        a small HTML page for a test extension to open
  GET  /__state       everything recorded so far: logins, requests, results

Every request is recorded with whether it carried a valid token, so a test can
tell "still logged in" from "had to log in again". --state-file keeps the
issued tokens across restarts. --host 0.0.0.0 makes it reachable at a LAN
address, which a browser sends through its proxy (127.0.0.1 it never does).
Test only: fixed demo credentials, no persistence beyond the state file.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "demo", "demo-pass"
STATE = {"logins": [], "requests": [], "tokens": [], "results": []}
LOCK = threading.Lock()
STATE_FILE = None


def persist():
    if STATE_FILE:
        with open(STATE_FILE, "w") as fh:
            json.dump(STATE, fh)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def log_error(self, fmt, *args):  # but say why a request could not be parsed (e.g. TLS sent to plain HTTP)
        print(f"error from {self.client_address[0]}: {fmt % args}"[:200], flush=True)

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else None

    def _authed(self):
        return self._bearer() in STATE["tokens"]

    def _record(self, method, path):
        with LOCK:
            STATE["requests"].append({"t": time.time(), "m": method, "p": path, "authed": self._authed()})
            persist()

    def do_OPTIONS(self):  # CORS preflight; a 204 must not carry a body
        self._record("OPTIONS", self.path.split("?")[0])
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/__state":
            return self._send(200, STATE)
        if path == "/runner":
            body = b"<!doctype html><title>runner</title><h1>runner tab</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/":
            return self._send(200, {"mock": "backend"})
        self._record("GET", path)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        self._record("POST", path)
        if path == "/api/login":
            if body.get("username") != USER or body.get("password") != PASSWORD:
                return self._send(401, {"error": "bad credentials"})
            issued = "tok-" + secrets.token_hex(8)
            with LOCK:
                STATE["tokens"].append(issued)
                STATE["logins"].append({"t": time.time(), "client": body.get("client")})
                persist()
            return self._send(200, {"data": {"access_token": issued}})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if path == "/api/results":
            items = body.get("items") or []
            with LOCK:
                STATE["results"].append({"t": time.time(), "page_url": body.get("page_url"), "items": len(items)})
                persist()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to be reachable at a LAN address (proxy-bypass tests)")
    ap.add_argument("--state-file")
    args = ap.parse_args()
    global STATE_FILE
    if args.state_file:
        STATE_FILE = args.state_file
        try:
            with open(STATE_FILE) as fh:
                STATE.update(json.load(fh))
        except FileNotFoundError:
            pass
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock backend on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
