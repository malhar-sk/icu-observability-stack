"""Tests for interface/mcp_server.py, the MCP server written from scratch. Three layers:

  * the protocol logic, called directly (every rule about what is answered to what);
  * the real program over real pipes, with nothing but the standard library (framing, bad input, what reaches stdout);
  * the official MCP client from Anthropic's SDK, as an independent referee that the wire format is right.
    Only this last layer needs the `mcp` package, and only to run the tests: the server never imports it. Without it,
    those tests are skipped.  To run them too:  pip install mcp

A fake VictoriaMetrics from test_fleet stands in for the stack. Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_interface as ti  # noqa: E402  (the module, not its classes, so unittest does not run them twice)
from test_fleet import FakeVM, WithFakeVM, dev  # noqa: E402

from interface import mcp_server, registry  # noqa: E402
from interface.mcp_server import Protocol, VERSIONS  # noqa: E402

ROOT = ti.ROOT
SERVER = os.path.join(ROOT, "interface", "mcp_server.py")
NEWEST = VERSIONS[-1]
HAVE_MCP = importlib.util.find_spec("mcp") is not None
BARE_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}


def rpc(method, request_id=1, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def ready(version=NEWEST):
    protocol = Protocol()
    answer = protocol.handle(rpc("initialize", protocolVersion=version, capabilities={}, clientInfo={"name": "t", "version": "0"}))
    assert "result" in answer, answer
    return protocol


def call(protocol, name, arguments=None, request_id=2):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return protocol.handle(rpc("tools/call", request_id, **params))


# ----------------------------------------------------------------------------- 1. the protocol logic
class TheHandshake(unittest.TestCase):
    def test_the_answer_says_what_this_server_is_and_can_do(self):
        answer = Protocol().handle(rpc("initialize", 7, protocolVersion=NEWEST, capabilities={}))
        self.assertEqual(answer["id"], 7)
        result = answer["result"]
        self.assertEqual(result["protocolVersion"], NEWEST)
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}}, "tools only: nothing else is offered")
        self.assertEqual(result["serverInfo"]["name"], "icu-fleet")
        self.assertIn("cannot change anything", result["instructions"])

    def test_each_supported_version_is_agreed_to(self):
        for version in VERSIONS:
            self.assertEqual(Protocol().handle(rpc("initialize", protocolVersion=version))["result"]["protocolVersion"], version)

    def test_an_unknown_version_is_answered_with_our_newest(self):
        for version in ("1999-01-01", "2099-12-31", "latest", ""):
            self.assertEqual(Protocol().handle(rpc("initialize", protocolVersion=version))["result"]["protocolVersion"], NEWEST)

    def test_the_version_is_required(self):
        for params in ({}, {"protocolVersion": 5}, {"protocolVersion": None}):
            answer = Protocol().handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
            self.assertEqual(answer["error"]["code"], mcp_server.INVALID_PARAMS, params)

    def test_initializing_twice_is_refused(self):
        p = ready()
        self.assertEqual(p.handle(rpc("initialize", 9, protocolVersion=NEWEST))["error"]["code"], mcp_server.INVALID_REQUEST)

    def test_nothing_but_ping_works_before_initialize(self):
        p = Protocol()
        self.assertEqual(p.handle(rpc("ping"))["result"], {})
        for method in ("tools/list", "tools/call"):
            answer = p.handle(rpc(method))
            self.assertEqual(answer["error"]["code"], mcp_server.INVALID_REQUEST, method)
            self.assertIn("initialize", answer["error"]["message"])
        self.assertEqual(ready().handle(rpc("ping"))["result"], {})

    def test_what_is_not_offered_is_method_not_found_so_clients_fall_back(self):
        """server/discover is what the newest clients try first; any error makes them use initialize instead."""
        for protocol in (Protocol(), ready()):
            for method in ("server/discover", "resources/list", "prompts/list", "logging/setLevel", "completion/complete",
                           "tools/nonsense", "notifications/initialized"):
                answer = protocol.handle(rpc(method))
                self.assertEqual(answer["error"]["code"], mcp_server.METHOD_NOT_FOUND, method)
                self.assertEqual(answer["id"], 1)


class TheMessageRules(unittest.TestCase):
    def test_the_id_is_echoed_exactly(self):
        for request_id in (0, 7, -3, "abc", "", "7"):
            answer = Protocol().handle(rpc("ping", request_id))
            self.assertEqual(answer["id"], request_id)
            self.assertIs(type(answer["id"]), type(request_id))

    def test_an_id_that_is_not_a_number_or_text_is_refused(self):
        for bad in (True, None, 1.5, [], {}):
            answer = Protocol().handle({"jsonrpc": "2.0", "id": bad, "method": "ping"})
            self.assertEqual((answer["id"], answer["error"]["code"]), (None, mcp_server.INVALID_REQUEST), bad)

    def test_a_notification_is_never_answered_even_when_it_is_wrong(self):
        p = ready()
        for message in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
                        {"jsonrpc": "2.0", "method": "no/such/method"},
                        {"jsonrpc": "2.0", "method": "tools/call", "params": [1, 2]},
                        {"jsonrpc": "2.0", "method": "tools/call"}):
            self.assertIsNone(p.handle(message), message)

    def test_what_is_not_one_json_rpc_message_is_refused_with_a_null_id(self):
        for bad in ([], [rpc("ping")], 5, "ping", None, {}, {"id": 1, "method": "ping"}, {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": 1, "method": 5}):
            answer = Protocol().handle(bad)
            self.assertEqual((answer["id"], answer["error"]["code"]), (None, mcp_server.INVALID_REQUEST), bad)

    def test_an_answer_from_the_client_is_ignored(self):
        p = ready()
        self.assertIsNone(p.handle({"jsonrpc": "2.0", "id": 3, "result": {}}))
        self.assertIsNone(p.handle({"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "x"}}))

    def test_params_must_be_an_object(self):
        answer = ready().handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": [1]})
        self.assertEqual((answer["id"], answer["error"]["code"]), (4, mcp_server.INVALID_PARAMS))

    def test_a_bug_inside_the_protocol_is_an_internal_error_with_no_details(self):
        p = ready()
        with mock.patch.object(registry, "enabled", side_effect=RuntimeError("secret /path detail")), \
             mock.patch("sys.stderr"):
            answer = p.handle(rpc("tools/list", 5))
        self.assertEqual((answer["id"], answer["error"]["code"]), (5, -32603))
        self.assertNotIn("secret", json.dumps(answer))


class TheToolList(unittest.TestCase):
    def test_every_real_feature_is_a_read_only_tool_with_a_description_and_schema(self):
        tools = ready().handle(rpc("tools/list"))["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(ti.REAL_FEATURES))
        for t in tools:
            self.assertEqual(t["annotations"], {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                                "openWorldHint": False}, t["name"])
            self.assertGreater(len(t["description"]), 40, t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIs(t["inputSchema"]["additionalProperties"], False)
            self.assertEqual(t["outputSchema"], {"type": "object"})

    def test_older_clients_are_not_sent_fields_they_do_not_know(self):
        old = ready("2024-11-05").handle(rpc("tools/list"))["result"]["tools"][0]
        self.assertEqual(set(old), {"name", "description", "inputSchema"})
        middle = ready("2025-03-26").handle(rpc("tools/list"))["result"]["tools"][0]
        self.assertIn("annotations", middle)
        self.assertNotIn("outputSchema", middle)

    def test_disabling_a_feature_removes_its_tool_and_its_calls(self):
        with mock.patch.dict(os.environ, {registry.DISABLE_ENV: "get_device"}):
            p = ready()
            names = {t["name"] for t in p.handle(rpc("tools/list"))["result"]["tools"]}
            self.assertNotIn("get_device", names)
            self.assertEqual(len(names), len(ti.REAL_FEATURES) - 1)
            self.assertEqual(call(p, "get_device", {"device_id": "x"})["error"]["code"], mcp_server.INVALID_PARAMS)


class TheToolSchemasFollowTheFunctions(ti.Clean):
    def test_types_defaults_and_required_come_from_the_signature(self):
        @registry.feature
        def probe(name: str, count: int, ratio: float = 0.5, loud: bool = False, label: str = "x") -> dict:
            """Doc."""
            return {}
        tool = ready().handle(rpc("tools/list"))["result"]["tools"][0]
        self.assertEqual(tool["inputSchema"], {
            "type": "object", "additionalProperties": False, "required": ["name", "count"],
            "properties": {"name": {"type": "string"}, "count": {"type": "integer"},
                           "ratio": {"type": "number", "default": 0.5}, "loud": {"type": "boolean", "default": False},
                           "label": {"type": "string", "default": "x"}}})

    def test_a_feature_with_no_parameters_has_no_required_list(self):
        @registry.feature
        def bare() -> dict:
            """Doc."""
            return {}
        schema = ready().handle(rpc("tools/list"))["result"]["tools"][0]["inputSchema"]
        self.assertEqual(schema, {"type": "object", "properties": {}, "additionalProperties": False})

    def test_a_feature_added_later_needs_no_change_here(self):
        @registry.feature
        def one() -> dict:
            """Doc."""
            return {"n": 1}

        @registry.feature
        def two() -> dict:
            """Doc."""
            return {"n": 2}
        p = ready()
        self.assertEqual([t["name"] for t in p.handle(rpc("tools/list"))["result"]["tools"]], ["one", "two"])
        self.assertEqual(call(p, "two")["result"]["structuredContent"], {"n": 2})


class TheToolCalls(ti.WithStack):
    def test_an_answer_is_text_and_structured_data_that_agree(self):
        self.devices(**{"kitchen-pi": dev(age=2)})
        result = call(ready(), "list_devices")["result"]
        self.assertIs(result["isError"], False)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(result["structuredContent"]["devices"][0]["device_id"], "kitchen-pi")

    def test_older_clients_get_the_text_only(self):
        result = call(ready("2024-11-05"), "stack_status")["result"]
        self.assertNotIn("structuredContent", result)
        self.assertIn("services", json.loads(result["content"][0]["text"]))

    def test_arguments_reach_the_feature(self):
        self.devices(up=dev(age=2), down=dev(age=900))
        got = call(ready(), "list_devices", {"state": "offline"})["result"]["structuredContent"]
        self.assertEqual([d["device_id"] for d in got["devices"]], ["down"])
        one = call(ready(), "get_device", {"device_id": "up"})["result"]["structuredContent"]
        self.assertTrue(one["found"])

    def test_a_bad_call_is_a_result_the_assistant_can_read_and_correct(self):
        p = ready()
        for name, arguments, words in [
                ("get_device", None, "needs: device_id"), ("get_device", {}, "needs: device_id"),
                ("get_device", {"device_id": 5}, "'device_id' must be text"),
                ("get_device", {"device_id": "x", "colour": 1}, "no parameter colour"),
                ("get_device", ["x"], "must be an object"), ("get_device", "x", "must be an object"),
                ("get_device", {"device_id": "a" * 300}, "too long"),
                ("list_devices", {"state": "sleeping"}, "state must be one of"),
                ("list_devices", {"state": True}, "'state' must be text"),
                ("how_to_add_device", {"kind": "usb"}, "kind must be one of")]:
            result = call(p, name, arguments)["result"]
            self.assertIs(result["isError"], True, (name, arguments))
            self.assertIn(words, result["content"][0]["text"], (name, arguments))
            self.assertNotIn("structuredContent", result)

    def test_a_missing_or_unknown_tool_is_a_protocol_error(self):
        p = ready()
        for params in ({}, {"name": 5}, {"name": None}):
            self.assertEqual(p.handle(rpc("tools/call", 3, **params))["error"]["code"], mcp_server.INVALID_PARAMS, params)
        answer = call(p, "delete_everything")
        self.assertEqual(answer["error"]["code"], mcp_server.INVALID_PARAMS)
        self.assertIn("unknown tool: delete_everything", answer["error"]["message"])

    def test_a_stopped_stack_is_explained(self):
        FakeVM.state["healthy"] = False
        result = call(ready(), "list_devices")["result"]
        self.assertTrue(result["isError"])
        self.assertIn("./fleet up", result["content"][0]["text"])

    def test_a_bug_is_a_generic_message_and_the_details_stay_in_the_log(self):
        with mock.patch.object(registry, "call", side_effect=RuntimeError("detail at /etc/very/private")), \
             mock.patch("sys.stderr", new_callable=ti.io.StringIO) as log:
            result = call(ready(), "stack_status")["result"]
        self.assertTrue(result["isError"])
        self.assertIn("internal error", result["content"][0]["text"])
        self.assertNotIn("/etc/very/private", json.dumps(result))
        self.assertIn("/etc/very/private", log.getvalue())

    def test_a_secret_is_never_sent_to_the_assistant(self):
        self.env(f"MQTT_PASSWORD={ti.SECRET}\n")
        self.devices(**{"x": dev(host=ti.SECRET)})
        with mock.patch("sys.stderr", new_callable=ti.io.StringIO) as log:
            for name, arguments in (("list_devices", None), ("get_device", {"device_id": "x"})):
                result = call(ready(), name, arguments)["result"]
                self.assertTrue(result["isError"], name)
                self.assertNotIn(ti.SECRET, json.dumps(result), name)
            self.assertNotIn(ti.SECRET, log.getvalue())


# ----------------------------------------------------------------------------- 2. the real program over real pipes
def lines(raw):
    return [json.loads(l) for l in raw.decode("ascii").splitlines()]


class OnTheWire(WithFakeVM):
    """The server as a separate program. Input is written, the pipe is closed, and the output is read."""
    python_flags = ()

    def run_server(self, *messages, env=None, flags=None, args=()):
        data = b"".join(m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages)
        r = subprocess.run([sys.executable, *(self.python_flags if flags is None else flags), SERVER, *args], input=data,
                           capture_output=True, timeout=60, env={**BARE_ENV, "FLEET_VM_URL": self.url, **(env or {})})
        return r

    HELLO = [rpc("initialize", 1, protocolVersion=NEWEST, capabilities={}), {"jsonrpc": "2.0", "method": "notifications/initialized"}]

    def test_a_whole_conversation(self):
        self.devices(**{"kitchen-pi": dev(age=2)})
        r = self.run_server(*self.HELLO, rpc("tools/list", 2), rpc("tools/call", 3, name="list_devices", arguments={}),
                            rpc("ping", 4))
        self.assertEqual(r.returncode, 0, r.stderr)
        answers = lines(r.stdout)
        self.assertEqual([a["id"] for a in answers], [1, 2, 3, 4], "one answer per request, none for the notification")
        self.assertEqual(answers[0]["result"]["protocolVersion"], NEWEST)
        self.assertEqual(len(answers[1]["result"]["tools"]), len(ti.REAL_FEATURES))
        self.assertEqual(answers[2]["result"]["structuredContent"]["devices"][0]["device_id"], "kitchen-pi")
        self.assertEqual(answers[3]["result"], {})
        self.assertIn(b"ready", r.stderr)

    def test_it_needs_no_packages_at_all(self):
        """python -S leaves out everything that was pip-installed. It is the whole point of writing this ourselves."""
        r = self.run_server(*self.HELLO, rpc("tools/list", 2), flags=("-S",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(lines(r.stdout)[1]["result"]["tools"]), len(ti.REAL_FEATURES))

    def test_bad_input_gets_an_error_and_the_next_message_is_still_answered(self):
        r = self.run_server(*self.HELLO, b"this is not json\n", b"{\n", b'{"a": NaN}\n', b"\xff\xfe\n", b"[" * 200000 + b"\n",
                            b"\n", b"   \n", rpc("ping", 9))
        self.assertEqual(r.returncode, 0, r.stderr)
        answers = lines(r.stdout)
        self.assertEqual([a["error"]["code"] for a in answers[1:6]], [mcp_server.PARSE_ERROR] * 5)
        self.assertTrue(all(a["id"] is None for a in answers[1:6]))
        self.assertEqual((answers[-1]["id"], answers[-1]["result"]), (9, {}))
        self.assertEqual(len(answers), 7, "blank lines get no answer")

    def test_a_message_that_is_too_large_is_skipped_and_the_connection_survives(self):
        huge = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * (mcp_server.MAX_LINE + 5000) + b'"}}\n'
        r = self.run_server(huge, rpc("ping", 2))
        answers = lines(r.stdout)
        self.assertEqual(answers[0]["error"]["code"], mcp_server.INVALID_REQUEST)
        self.assertIn("too large", answers[0]["error"]["message"])
        self.assertEqual((answers[1]["id"], answers[1]["result"]), (2, {}))
        self.assertEqual(len(answers), 2)

    def test_line_endings_and_a_missing_final_newline_are_fine(self):
        r = self.run_server(json.dumps(rpc("ping", 1)).encode() + b"\r\n", json.dumps(rpc("ping", 2)).encode())
        self.assertEqual([a["id"] for a in lines(r.stdout)], [1, 2])

    def test_the_output_is_one_ascii_line_per_message_whatever_the_text(self):
        self.devices(**{"café": dev()})
        r = self.run_server(*self.HELLO, rpc("tools/call", 2, name="get_device", arguments={"device_id": "naïve \U0001f600 \n  "}),
                            rpc("tools/call", 3, name="get_device", arguments={"device_id": "\ud800"}),
                            rpc("tools/call", 4, name="get_device", arguments={"device_id": "café"}))
        r.stdout.decode("ascii")  # raises if a non-ASCII byte got out
        answers = lines(r.stdout)
        self.assertEqual(len(r.stdout.splitlines()), len(answers), "no message was split over several lines")
        text = json.loads(answers[1]["result"]["content"][0]["text"])
        self.assertEqual(text["device_id"], "naïve \U0001f600 \n  ", "the text survives the round trip")
        self.assertFalse(json.loads(answers[2]["result"]["content"][0]["text"])["found"], "a lone surrogate is fine")
        known = json.loads(answers[3]["result"]["content"][0]["text"])
        self.assertEqual((known["found"], known["device_id"]), (True, "café"), "a non-ASCII id goes in and out intact")

    def test_a_cut_off_message_at_the_end_is_an_error_not_a_hang(self):
        r = self.run_server(rpc("ping", 1), b'{"jsonrpc":"2.0","id":2,"method":"pi')
        answers = lines(r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual((answers[0]["id"], answers[1]["error"]["code"]), (1, mcp_server.PARSE_ERROR))

    def test_it_stops_when_the_client_closes_the_pipe(self):
        r = self.run_server()
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_nothing_but_answers_reaches_stdout_whatever_other_code_prints(self):
        """Stray prints, raw writes to fd 1 and child processes all land on stderr; none of them can read the client's messages."""
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "from interface import mcp_server\n"
            "reader, writer = mcp_server.claim_stdio()\n"
            "print('stray print'); sys.stdout.flush()\n"
            "os.write(1, b'raw write to fd 1\\n')\n"
            "os.system('echo from a child process')\n"
            "stolen = sys.stdin.read() + os.read(0, 100).decode()\n"
            "print('stolen=' + repr(stolen), file=sys.stderr)\n"
            "mcp_server.serve(mcp_server.Protocol(), reader, writer)\n")
        r = subprocess.run([sys.executable, "-c", script], input=json.dumps(rpc("ping", 5)).encode() + b"\n",
                           capture_output=True, timeout=60, env=BARE_ENV)
        self.assertEqual(lines(r.stdout), [{"jsonrpc": "2.0", "id": 5, "result": {}}], r.stderr)
        for expected in (b"stray print", b"raw write to fd 1", b"from a child process"):
            self.assertIn(expected, r.stderr)
        self.assertIn(b"stolen=''", r.stderr, "the client's messages could not be read by anything else")

    def test_a_typo_in_the_switch_stops_it_with_a_reason(self):
        r = self.run_server(env={registry.DISABLE_ENV: "get_devise"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"get_devise", r.stderr)
        self.assertIn(b"not a feature", r.stderr)
        self.assertEqual(r.stdout, b"")

    def test_print_config_gives_settings_to_paste(self):
        r = self.run_server(args=("--print-config",))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode()
        config = json.loads(out[out.index("{"):out.rindex("}") + 1])["mcpServers"]["icu-fleet"]
        self.assertEqual((config["command"], config["args"]), (sys.executable, [SERVER]))
        self.assertIn(f"claude mcp add icu-fleet -- {sys.executable} {SERVER}", out)


# ----------------------------------------------------------------------------- 3. the official client as referee
@unittest.skipUnless(HAVE_MCP, "the official 'mcp' package is not installed; it is only needed for these referee tests")
class TheOfficialClientAgrees(WithFakeVM):
    def session(self, mode="auto", env=None):
        from mcp import Client, StdioServerParameters
        # -S: the server runs without any installed package, so the client cannot be what makes it work
        params = StdioServerParameters(command=sys.executable, args=["-S", SERVER],
                                       env={**BARE_ENV, "FLEET_VM_URL": self.url, **(env or {})})
        return Client(params, mode=mode)

    def use(self, work, mode="auto", env=None):
        async def go():
            async with self.session(mode, env) as client:
                return await work(client)
        return asyncio.run(asyncio.wait_for(go(), 60))

    def test_it_connects_in_both_modes_and_agrees_on_the_version(self):
        async def work(client):
            return client.protocol_version, client.server_info.name, [t.name for t in (await client.list_tools()).tools]
        for mode in ("auto", "legacy"):  # auto first tries server/discover, which we answer with method-not-found
            version, name, tools = self.use(work, mode)
            self.assertEqual((version, name), (NEWEST, "icu-fleet"), mode)
            self.assertEqual(set(tools), set(ti.REAL_FEATURES), mode)

    def test_the_tool_descriptions_pass_the_clients_own_validation(self):
        async def work(client):
            return (await client.list_tools()).tools
        for tool in self.use(work):
            self.assertTrue(tool.annotations.read_only_hint, tool.name)
            self.assertFalse(tool.annotations.destructive_hint, tool.name)
            self.assertEqual(tool.output_schema, {"type": "object"})
        get_device = next(t for t in self.use(work) if t.name == "get_device")
        self.assertEqual(get_device.input_schema["required"], ["device_id"])

    def test_calls_return_text_and_structured_data(self):
        self.devices(**{"kitchen-pi": dev(age=2), "old": dev(age=900)})

        async def work(client):
            ok = await client.call_tool("list_devices", {"state": "offline"})
            bad = await client.call_tool("how_to_add_device", {"kind": "usb"})
            return ok, bad
        ok, bad = self.use(work)
        self.assertFalse(ok.is_error)
        self.assertEqual([d["device_id"] for d in ok.structured_content["devices"]], ["old"])
        self.assertEqual(json.loads(ok.content[0].text), ok.structured_content)
        self.assertTrue(bad.is_error)
        self.assertIn("kind must be one of: agent, mqtt, checkin, watch", bad.content[0].text)

    def test_the_client_enforces_our_output_schema_and_we_satisfy_it(self):
        async def work(client):
            return [await client.call_tool(t.name, {"device_id": "x"} if t.name == "get_device" else {})
                    for t in (await client.list_tools()).tools]
        FakeVM.state["healthy"] = True
        for result in self.use(work):
            self.assertFalse(result.is_error)

    def test_a_disabled_feature_is_not_offered(self):
        async def work(client):
            return {t.name for t in (await client.list_tools()).tools}
        self.assertEqual(self.use(work, env={registry.DISABLE_ENV: "how_to_add_device,get_device"}),
                         {"stack_status", "list_devices"})


# ----------------------------------------------------------------------------- 4. the same server over HTTP: POST /mcp
def post_mcp(port, message=None, body=None, headers=None, host_header=None, content_type="application/json", path="/mcp",
             length=None):
    """POST to /mcp. Returns (status, headers, parsed body or None)."""
    data = body if body is not None else json.dumps(message).encode()
    conn = ti.http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("POST", path, skip_host=True)
        conn.putheader("Host", host_header or f"localhost:{port}")
        if content_type:
            conn.putheader("Content-Type", content_type)
        if length is not False:
            conn.putheader("Content-Length", str(len(data) if length is None else length))
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.endheaders()
        if data and length is None:
            conn.send(data)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, dict(resp.getheaders()), (json.loads(raw) if raw else None)
    finally:
        conn.close()


class OverHttp(ti.Api):
    def post(self, message=None, **kw):
        return post_mcp(self.port, message, **kw)

    def result(self, method, version=NEWEST, request_id=1, **params):
        status, headers, body = self.post(rpc(method, request_id, **params), headers={"MCP-Protocol-Version": version})
        self.assertEqual(status, 200, body)
        return body["result"]


class TheHttpDoor(OverHttp):
    def test_a_conversation(self):
        self.devices(**{"kitchen-pi": dev(age=2)})
        status, headers, body = self.post(rpc("initialize", 1, protocolVersion=NEWEST, capabilities={}))
        self.assertEqual((status, body["result"]["protocolVersion"], body["id"]), (200, NEWEST, 1))
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertNotIn("Mcp-Session-Id", headers, "nothing is remembered between requests, so there is no session")
        status, _, empty = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, empty), (202, None))
        tools = self.result("tools/list")["tools"]
        self.assertEqual({t["name"] for t in tools}, set(ti.REAL_FEATURES))
        called = self.result("tools/call", name="list_devices", arguments={})
        self.assertEqual(called["structuredContent"]["devices"][0]["device_id"], "kitchen-pi")
        self.assertEqual(json.loads(called["content"][0]["text"]), called["structuredContent"])

    def test_every_request_stands_alone(self):
        """tools/list works with no initialize before it: there is no state to hold, and none to steal."""
        self.assertEqual(len(self.result("tools/list")["tools"]), len(ti.REAL_FEATURES))
        self.assertEqual(self.result("initialize", protocolVersion=NEWEST)["protocolVersion"], NEWEST)
        self.assertEqual(self.result("initialize", protocolVersion=NEWEST)["protocolVersion"], NEWEST, "twice is fine")

    def test_the_version_header_decides_what_the_answer_contains(self):
        self.devices(a=dev())
        newest = self.result("tools/call", name="stack_status", arguments={})
        self.assertIn("structuredContent", newest)
        status, _, body = self.post(rpc("tools/call", 2, name="stack_status", arguments={}))  # no header: assume 2025-03-26
        self.assertNotIn("structuredContent", body["result"])
        self.assertIn("annotations", self.result("tools/list", "2025-03-26")["tools"][0])
        self.assertNotIn("outputSchema", self.result("tools/list", "2025-03-26")["tools"][0])

    def test_an_unsupported_version_is_a_400_which_is_how_newer_clients_fall_back(self):
        for version in ("2026-07-28", "1999-01-01", "latest"):
            status, _, body = self.post(rpc("server/discover"), headers={"MCP-Protocol-Version": version})
            self.assertEqual(status, 400, version)
            self.assertIn("unsupported MCP-Protocol-Version", body["error"])

    def test_what_is_not_offered_is_method_not_found(self):
        status, _, body = self.post(rpc("server/discover"))
        self.assertEqual((status, body["error"]["code"]), (200, mcp_server.METHOD_NOT_FOUND))

    def test_bad_messages_are_refused_without_a_crash(self):
        for raw in (b"not json", b"{", b'{"a": NaN}', b"\xff\xfe", b"[" * 100000):
            status, _, body = self.post(body=raw)
            self.assertEqual((status, body["error"]["code"]), (400, mcp_server.PARSE_ERROR), raw[:20])
        status, _, body = self.post([rpc("ping")])  # a batch
        self.assertEqual((status, body["error"]["code"]), (200, mcp_server.INVALID_REQUEST))
        self.assertEqual(self.result("ping"), {}, "and the server is still fine")

    def test_the_body_must_be_json_and_have_a_length_within_the_limit(self):
        self.assertEqual(self.post(rpc("ping"), content_type="text/plain")[0], 415)
        self.assertEqual(self.post(rpc("ping"), content_type=None)[0], 415)
        self.assertEqual(self.post(rpc("ping"), length=False)[0], 411)
        status, _, _ = self.post(body=b"", length=mcp_server.MAX_LINE + 1)  # claims too much: refused before reading it
        self.assertEqual(status, 413)
        self.assertEqual(self.result("ping"), {})

    def test_a_web_page_is_refused_by_its_origin_header(self):
        for origin in ("https://evil.example", "http://localhost.evil.com", "http://10.0.0.5:8765", "null"):
            self.assertEqual(self.post(rpc("ping"), headers={"Origin": origin})[0], 403, origin)
        for origin in ("http://localhost:6274", "http://127.0.0.1:3000"):
            self.assertEqual(self.post(rpc("ping"), headers={"Origin": origin})[0], 200, origin)

    def test_a_foreign_host_name_is_refused(self):
        self.assertEqual(self.post(rpc("ping"), host_header="evil.example")[0], 403)

    def test_it_takes_post_only_and_the_rest_of_the_api_still_does_not(self):
        for method in ("GET", "DELETE", "PUT", "PATCH"):
            status, headers, _ = self.request("/mcp", method=method)
            self.assertEqual((status, headers["Allow"]), (405, "POST"), method)
        status, headers, _ = self.post(rpc("ping"), path="/v1/list_devices")
        self.assertEqual((status, headers["Allow"]), (405, "GET"))
        self.assertEqual(self.post(rpc("ping"), path="/mcp/")[0], 200)

    def test_the_two_doors_give_the_same_answer(self):
        self.devices(up=dev(age=2), down=dev(age=900))
        via_mcp = self.result("tools/call", name="list_devices", arguments={"state": "offline"})["structuredContent"]
        _, _, via_api = self.request("/v1/list_devices?state=offline")
        self.assertEqual(via_mcp, via_api)

    def test_a_problem_is_an_error_result_not_a_failed_request(self):
        result = self.result("tools/call", name="how_to_add_device", arguments={"kind": "usb"})
        self.assertTrue(result["isError"])
        self.assertIn("kind must be one of", result["content"][0]["text"])

    def test_a_secret_is_never_sent_and_nothing_of_the_call_is_logged(self):
        self.env(f"MQTT_PASSWORD={ti.SECRET}\n")
        self.devices(**{"x": dev(host=ti.SECRET)})
        result = self.result("tools/call", name="get_device", arguments={"device_id": "x"})
        self.assertTrue(result["isError"])
        self.assertNotIn(ti.SECRET, json.dumps(result))
        self.result("tools/call", name="get_device", arguments={"device_id": "my-private-device-name"})
        log = self.log.getvalue()
        self.assertNotIn(ti.SECRET, log)
        self.assertNotIn("my-private-device-name", log, "the body of a call is never logged")
        self.assertIn('"POST /mcp" 200', log)


class TheHttpDoorWithAToken(OverHttp):
    token = "mcp-token-" + "q" * 20

    def test_nothing_is_answered_without_the_token(self):
        calls = []
        with mock.patch.object(registry, "call", side_effect=lambda *a, **k: calls.append(a)):
            for headers in (None, {"Authorization": "Bearer wrong"}, {"Authorization": self.token},
                            {"Authorization": "Basic " + self.token}):
                status, response_headers, body = self.post(rpc("tools/call", 1, name="stack_status"), headers=headers)
                self.assertEqual(status, 401, headers)
                self.assertEqual(response_headers["WWW-Authenticate"], "Bearer")
            self.assertEqual(self.post(rpc("ping"), path=f"/mcp?token={self.token}")[0], 401, "not in the address")
        self.assertEqual(calls, [], "no feature ran for any refused request")

    def test_the_right_token_works(self):
        status, _, body = self.post(rpc("ping"), headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual((status, body["result"]), (200, {}))


@unittest.skipUnless(HAVE_MCP, "the official 'mcp' package is not installed; it is only needed for these referee tests")
class TheOfficialClientOverHttp(OverHttp):
    token = "ref-token-" + "w" * 20

    def connect(self, mode="auto", token=None, then=None):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async def go():
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token or self.token}"})
            async with Client(streamable_http_client(f"http://127.0.0.1:{self.port}/mcp", http_client=http), mode=mode) as client:
                return await then(client)
        return asyncio.run(asyncio.wait_for(go(), 60))

    def test_it_connects_in_both_modes_lists_and_calls(self):
        self.devices(**{"kitchen-pi": dev(age=2)})

        async def work(client):
            tools = await client.list_tools()
            ok = await client.call_tool("get_device", {"device_id": "kitchen-pi"})
            bad = await client.call_tool("how_to_add_device", {"kind": "usb"})
            return client.protocol_version, {t.name for t in tools.tools}, ok, bad
        for mode in ("auto", "legacy"):
            version, names, ok, bad = self.connect(mode, then=work)
            self.assertEqual((version, names), (NEWEST, set(ti.REAL_FEATURES)), mode)
            self.assertEqual(ok.structured_content["state"], "ONLINE", mode)
            self.assertTrue(bad.is_error, mode)

    def test_a_wrong_token_cannot_connect(self):
        async def work(client):
            return await client.list_tools()
        with self.assertRaises(BaseException):
            self.connect("legacy", token="not-the-token", then=work)


if __name__ == "__main__":
    unittest.main()
