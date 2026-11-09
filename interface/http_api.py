#!/usr/bin/env python3
"""A read-only HTTP server with two doors onto the same questions: a JSON API for scripts, and MCP for AI assistants.

  python3 interface/http_api.py                      listens on 127.0.0.1:8765 (this machine only)
  curl http://127.0.0.1:8765/v1                      the list of questions and their parameters
  curl http://127.0.0.1:8765/v1/list_devices?state=offline
  POST http://127.0.0.1:8765/mcp                     the MCP server over HTTP, so an assistant on another computer can connect
  python3 interface/http_api.py --print-mcp-config   what to give an assistant to connect to /mcp

Safe by default, because it shows your fleet to whoever can reach it:
  * GET only; nothing it offers can change anything;
  * it listens on this machine only. To listen on any other address you must set FLEET_API_TOKEN (16+ characters),
    and every request must then send  Authorization: Bearer <token>  (the token is never taken from the command line);
  * on a loopback address the Host header must be localhost, 127.0.0.1 or [::1], which stops a web page from
    reading it through DNS rebinding;
  * /mcp is stateless (nothing is remembered between requests), takes POST only, and refuses any request that carries an
    Origin header (a browser page) unless it is a localhost page.
Plain HTTP, no TLS: put it behind something that adds TLS before sending the token across a network you do not trust.
Run it on the machine that hosts the stack. Python 3.9+, standard library only. See docs/INTERFACE.md.
"""
import argparse
import hmac
import ipaddress
import json
import os
import socket
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from interface import mcp_server, registry  # noqa: E402
from interface.registry import FeatureError  # noqa: E402

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765
MIN_TOKEN_LENGTH = 16
MAX_REQUEST_LINE = 2000
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]", "::1")
MCP_PATH = "/mcp"


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_config(host, token):
    """The rules for listening. Returns a message when they are broken, else None."""
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        return f"FLEET_API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters. Make one with: python3 -c \"import secrets; print(secrets.token_hex(16))\""
    if not is_loopback(host) and not token:
        return (f"refusing to listen on {host}: anyone who can reach it could read your fleet. Set FLEET_API_TOKEN "
                f"(16+ characters) and send it as 'Authorization: Bearer <token>', or listen on {DEFAULT_HOST} only.")
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "fleet-interface"
    timeout = 10  # seconds a connection may sit idle, so a stalled client cannot hold a thread for ever
    token = None  # set by make_server
    loopback = True

    def log_message(self, fmt, *args):
        line = fmt % args
        if self.token:
            line = line.replace(self.token, "<token>")  # e.g. the base class quotes a malformed request line as it came
        sys.stderr.write("%s %s\n" % (self.address_string(), line))

    def log_request(self, code="-", size="-"):
        # Method, path and status only. A query string can carry anything (a ?token=... put there by mistake, a device
        # id), and a log is a file that outlives the request.
        # (for a request too broken to parse, command and path do not exist yet)
        self.log_message('"%s %s" %s', getattr(self, "command", None) or "-", (getattr(self, "path", None) or "-").split("?", 1)[0], code)

    # ------------------------------------------------------------ answering
    def reply(self, code, body, extra=None):
        data = json.dumps(body, indent=2).encode() + b"\n"
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":  # a HEAD answer has headers only
            self.wfile.write(data)

    def refuse(self, code, message, extra=None):
        self.reply(code, {"error": message}, extra)

    # ------------------------------------------------------------ the checks, in order
    def allowed(self):
        """True when the request may go on; otherwise the refusal has been sent."""
        if len(self.requestline) > MAX_REQUEST_LINE:
            self.refuse(414, "the request is too long")
            return False
        if self.loopback:
            host = self.headers.get("Host", "")
            name = host.rsplit(":", 1)[0] if not host.endswith("]") and ":" in host else host
            if name.lower() not in LOOPBACK_NAMES:
                self.refuse(403, "this API only answers to localhost, 127.0.0.1 or [::1]")
                return False
        if self.token:
            given = self.headers.get("Authorization", "")
            expected = f"Bearer {self.token}"
            if not hmac.compare_digest(given.encode(), expected.encode()):
                self.refuse(401, "send the access token as 'Authorization: Bearer <token>'", {"WWW-Authenticate": "Bearer"})
                return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/":
                return self.reply(200, {"name": "fleet interface", "read_only": True, "questions": "/v1",
                                        "mcp": MCP_PATH, "docs": "docs/INTERFACE.md"})
            if path == "/v1":
                return self.reply(200, {"read_only": True, "mcp": MCP_PATH,
                                        "features": [registry.describe(f) for f in registry.enabled()]})
            if path == MCP_PATH:
                return self.refuse(405, "this server does not stream: POST JSON-RPC messages to /mcp", {"Allow": "POST"})
            if path.startswith("/v1/"):
                return self.run_feature(path[len("/v1/"):], url.query)
            self.refuse(404, "unknown address; see /v1 for what can be asked")
        except FeatureError as exc:
            self.refuse(exc.code, str(exc))
        except Exception:  # a bug: say so briefly, keep the details in the server's log
            traceback.print_exc(file=sys.stderr)
            self.refuse(500, "internal error; the details are in this server's log")

    def run_feature(self, name, query):
        f = registry.get(name)
        if f is None:
            raise FeatureError(f"no feature named '{name}'; see /v1", code=404)
        try:
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            raise FeatureError("too many parameters")
        raw = {}
        for key, value in pairs:
            if key in raw:
                raise FeatureError(f"'{key}' was given twice")
            raw[key] = value
        self.reply(200, registry.call(name, **registry.coerce(f, raw)))

    def not_allowed(self):
        if urllib.parse.urlparse(self.path).path.rstrip("/") == MCP_PATH:
            return self.refuse(405, "/mcp takes POST only", {"Allow": "POST"})
        self.refuse(405, "this API is read-only: GET only", {"Allow": "GET"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = not_allowed

    # ------------------------------------------------------------ MCP over HTTP (see mcp_server.Protocol)
    def do_POST(self):
        if not self.allowed():
            return
        if urllib.parse.urlparse(self.path).path.rstrip("/") != MCP_PATH:
            return self.not_allowed()
        try:
            self.mcp_post()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.refuse(500, "internal error; the details are in this server's log")

    def mcp_post(self):
        origin = self.headers.get("Origin")
        if origin is not None and (urllib.parse.urlparse(origin).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
            return self.refuse(403, "requests from a web page are refused (Origin header); an MCP client does not send one")
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self.refuse(415, "send the message as Content-Type: application/json")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            self.close_connection = True
            return self.refuse(411, "send a Content-Length (chunked bodies are not accepted)")
        if int(length) > mcp_server.MAX_LINE:
            self.close_connection = True
            return self.refuse(413, f"message too large (limit {mcp_server.MAX_LINE} bytes)")
        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in mcp_server.VERSIONS:
            return self.refuse(400, f"unsupported MCP-Protocol-Version; supported: {', '.join(mcp_server.VERSIONS)}")
        body = self.rfile.read(int(length))
        try:
            message = mcp_server.decode(body)
        except ValueError:
            return self.reply(400, mcp_server.error_response(None, mcp_server.PARSE_ERROR, "not valid JSON"))
        response = mcp_server.Protocol(stateless=True, version=version).handle(message)
        if response is None:  # a notification, or an answer from the client: accepted, nothing to say
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.reply(200, response)


def make_server(host, port, token=None):
    problem = check_config(host, token)
    if problem:
        raise SystemExit(f"fleet interface: {problem}")
    handler = type("BoundHandler", (Handler,), {"token": token, "loopback": is_loopback(host)})
    server_class = type("Server", (ThreadingHTTPServer,), {
        "daemon_threads": True,
        "address_family": socket.AF_INET6 if ":" in host else socket.AF_INET,
    })
    return server_class((host, port), handler)


def print_mcp_config(host, port):
    """Tell a person how to point an assistant at /mcp. Never prints the token itself."""
    from interface import fleetlink
    shown = host if host not in ("0.0.0.0", "::", "") else (fleetlink.server_address() or "SERVER-ADDRESS")
    url = f"http://{shown}:{port}/mcp"
    print("An MCP client that speaks MCP over HTTP needs two things:")
    print(f"  URL      {url}")
    print("  Header   Authorization: Bearer <your FLEET_API_TOKEN>   (required unless the server listens on this machine only)")
    print("\nStart the server for other computers with a token only you know:")
    print("  export FLEET_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')")
    print(f"  python3 interface/http_api.py --host 0.0.0.0 --port {port}")
    print("\nFor Claude Code the command looks like this (the exact options differ between client versions: check yours):")
    print(f'  claude mcp add --transport http icu-fleet {url} --header "Authorization: Bearer <your FLEET_API_TOKEN>"')
    print("\nPlain HTTP: anyone who can watch your network can read the token. Use it on a network you trust, or put TLS in front.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only HTTP JSON API for the fleet. See docs/INTERFACE.md.")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"address to listen on (default {DEFAULT_HOST}; anything else needs FLEET_API_TOKEN)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    p.add_argument("--print-mcp-config", action="store_true",
                   help="print what an AI assistant on another computer needs to connect to /mcp, then exit")
    args = p.parse_args(argv)
    if args.print_mcp_config:
        return print_mcp_config(args.host, args.port)
    token = os.environ.get("FLEET_API_TOKEN") or None
    try:
        names = [f.name for f in registry.load_features()]
    except ValueError as exc:
        raise SystemExit(f"fleet interface: {exc}")
    server = make_server(args.host, args.port, token)
    shown = args.host if ":" not in args.host else f"[{args.host}]"
    print(f"fleet interface (read-only API) on http://{shown}:{args.port}/v1  features: {', '.join(names)}"
          + ("  (token required)" if token else ""), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
