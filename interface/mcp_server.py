#!/usr/bin/env python3
"""A read-only MCP server, written from scratch with the standard library: no packages to install, nothing to update.

  python3 interface/mcp_server.py                 talk MCP over stdin/stdout (an MCP client starts this for you)
  python3 interface/mcp_server.py --print-config  print the settings to paste into your MCP client

It opens no network port: the client starts it as a program on this machine and talks to it through pipes.
Every tool is built from interface/features/ (the same list the HTTP API uses) and marked read-only.

What it speaks: JSON-RPC 2.0, one message per line, with the MCP methods a read-only tool server needs:
initialize, ping, tools/list and tools/call (protocol versions 2024-11-05 to 2025-11-25).
What it does not: resources, prompts, logging, sampling, progress, cancellation (every call is short and they run one
at a time), batches, and any HTTP transport. A client that asks for those gets a standard "method not found".

The file has three parts, each independent of the others:
  1. TOOLS      turns the feature list into MCP tool descriptions, and a tool call into a feature call
  2. PROTOCOL   decides what to answer to each message; no input or output, so it is easy to test
  3. TRANSPORT  reads lines from stdin, writes lines to stdout, and keeps everything else off the wire
Python 3.9+. See docs/INTERFACE.md.
"""
import argparse
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from interface import registry  # noqa: E402

NAME, VERSION = "icu-fleet", "0.1.0"
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")  # oldest to newest; the newest is what we offer
MAX_LINE = 1_000_000  # bytes in one incoming message; a client has no business sending more
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INSTRUCTIONS = (
    "Read-only access to a device-monitoring stack (VictoriaMetrics, Grafana and an OpenTelemetry collector). "
    "Use stack_status first to see whether the server is up and which inputs are on, list_devices and get_device to see "
    "the devices, and how_to_add_device for the exact steps to add one. After a device is set up, call get_device to "
    "check it arrived. These tools cannot change anything: commands marked 'server' in how_to_add_device are run by a "
    "person on the machine that hosts the stack. Never ask for or invent passwords; they are not available here."
)


def at_least(version, wanted):
    """True when protocol `version` is `wanted` or newer. Versions are an enumerated list, not text to compare."""
    return VERSIONS.index(version) >= VERSIONS.index(wanted)


# ================================================================ 1. TOOLS
JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def describe_tool(f, version):
    """One feature as an MCP tool. Fields a client of an older protocol version does not know are left out."""
    properties, required = {}, []
    for p in f.params:
        properties[p.name] = {"type": JSON_TYPES[p.type]}
        if p.required:
            required.append(p.name)
        else:
            properties[p.name]["default"] = p.default
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    tool = {"name": f.name, "description": f.description, "inputSchema": schema}
    if at_least(version, "2025-03-26"):
        tool["annotations"] = dict(READ_ONLY)
    if at_least(version, "2025-06-18"):
        tool["outputSchema"] = {"type": "object"}
    return tool


def tool_result(text, is_error, structured=None, version=VERSIONS[-1]):
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None and at_least(version, "2025-06-18"):
        result["structuredContent"] = structured
    return result


def run_tool(f, arguments, version):
    """Run a feature for a tool call. A problem with the call is a result the assistant can read and correct (isError);
    only an unknown tool is a protocol error. Details of a bug stay in this program's log, never in the answer."""
    try:
        answer = registry.call(f.name, **registry.from_json(f, arguments))
    except registry.FeatureError as exc:
        return tool_result(str(exc), True)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return tool_result("internal error; the details are in the MCP server's log (stderr)", True)
    return tool_result(json.dumps(answer, indent=2), False, answer, version)


# ================================================================ 2. PROTOCOL
class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def error_response(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class Protocol:
    """One client connection. handle(message) takes a decoded JSON message and returns the response to send, or None."""

    def __init__(self):
        self.version = None  # set when initialize succeeds

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error_response(None, INVALID_REQUEST, "each message must be one JSON-RPC 2.0 object (batches are not supported)")
        method = message.get("method")
        if method is None:
            if "id" in message and ("result" in message or "error" in message):
                return None  # an answer to something we never asked: nothing to do
            return error_response(None, INVALID_REQUEST, "no method")
        if not isinstance(method, str):
            return error_response(None, INVALID_REQUEST, "method must be text")
        is_request = "id" in message
        request_id = message.get("id")
        if is_request and (isinstance(request_id, bool) or not isinstance(request_id, (int, str))):
            return error_response(None, INVALID_REQUEST, "id must be a number or text")
        params = message.get("params", {})
        try:
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            if not is_request:
                self.notification(method)
                return None  # a notification is never answered, not even to say it was wrong
            return {"jsonrpc": "2.0", "id": request_id, "result": self.request(method, params)}
        except RpcError as exc:
            return error_response(request_id, exc.code, exc.message) if is_request else None
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return error_response(request_id, -32603, "internal error") if is_request else None

    def notification(self, method):
        pass  # notifications/initialized, notifications/cancelled and the rest need no action here

    def request(self, method, params):
        if method == "ping":
            return {}
        if method == "initialize":
            return self.initialize(params)
        if method not in ("tools/list", "tools/call"):
            raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")  # includes server/discover: clients fall back
        if self.version is None:
            raise RpcError(INVALID_REQUEST, "send initialize first")
        if method == "tools/list":
            return {"tools": [describe_tool(f, self.version) for f in registry.enabled()]}
        return self.call_tool(params)

    def initialize(self, params):
        if self.version is not None:
            raise RpcError(INVALID_REQUEST, "already initialized")
        asked = params.get("protocolVersion")
        if not isinstance(asked, str):
            raise RpcError(INVALID_PARAMS, "protocolVersion is required")
        self.version = asked if asked in VERSIONS else VERSIONS[-1]  # an unknown version gets our newest; the client decides
        return {"protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NAME, "version": VERSION}, "instructions": INSTRUCTIONS}

    def call_tool(self, params):
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "tools/call needs the tool's name")
        f = registry.get(name)
        if f is None:
            raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")
        return run_tool(f, params.get("arguments"), self.version)


# ================================================================ 3. TRANSPORT
def claim_stdio():
    """Take the real stdin and stdout for the protocol and point the normal ones somewhere harmless: stdout at stderr and
    stdin at nothing. Anything else that writes to stdout (a stray print, a library, a child process) then cannot corrupt
    the stream a client is parsing, and nothing else can read the client's messages. Done on the file descriptors, so it
    holds for code that bypasses sys.stdout too. Returns (reader, writer)."""
    reader = os.fdopen(os.dup(0), "rb")
    writer = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    return reader, writer


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def decode(raw):
    """One line of bytes to a message. Raises ValueError for anything that is not strict JSON (NaN, bad UTF-8, too deeply
    nested): the server answers with a parse error and carries on."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("nested too deeply")


def send(writer, message):
    # ascii-only, no raw newline anywhere: a message is exactly one line, whatever its text contains
    writer.write(json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n")
    writer.flush()


def serve(protocol, reader, writer):
    """Answer messages until the client closes the pipe. A bad line gets an error and the next one is read normally."""
    while True:
        line = reader.readline(MAX_LINE + 1)
        if not line:
            return  # the client closed its end: that is how it says goodbye
        if len(line) > MAX_LINE and not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):  # skip the rest of the oversized message
                line = reader.readline(MAX_LINE)
            send(writer, error_response(None, INVALID_REQUEST, f"message too large (limit {MAX_LINE} bytes)"))
            continue
        if not line.strip():
            continue
        try:
            message = decode(line)
        except ValueError:
            send(writer, error_response(None, PARSE_ERROR, "not valid JSON"))
            continue
        response = protocol.handle(message)
        if response is not None:
            send(writer, response)


def config():
    """What to put in an MCP client's settings so it starts this server."""
    command, script = sys.executable, os.path.abspath(__file__)
    return {"mcpServers": {NAME: {"command": command, "args": [script]}},
            "claude_code_command": f"claude mcp add {NAME} -- {command} {script}"}


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only MCP server for the fleet. See docs/INTERFACE.md.")
    p.add_argument("--print-config", action="store_true", help="print the settings for an MCP client and exit")
    args = p.parse_args(argv)
    if args.print_config:
        c = config()
        print("Add this to your MCP client's settings (Claude Desktop: claude_desktop_config.json):")
        print(json.dumps({"mcpServers": c["mcpServers"]}, indent=2))
        print("\nOr, for Claude Code:")
        print(f"  {c['claude_code_command']}")
        return 0
    try:
        names = [f.name for f in registry.load_features()]  # a mistake in FLEET_INTERFACE_DISABLE stops us here
    except ValueError as exc:
        sys.exit(f"fleet interface: {exc}")
    reader, writer = claim_stdio()
    print(f"fleet interface (read-only MCP server) ready; tools: {', '.join(names)}", file=sys.stderr, flush=True)
    try:
        serve(Protocol(), reader, writer)
    except (BrokenPipeError, KeyboardInterrupt):
        pass  # the client went away
    return 0


if __name__ == "__main__":
    sys.exit(main())
