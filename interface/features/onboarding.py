"""Guidance for setting a device up: which of the four ways fits, and the exact steps for it.

This is guidance, not action: the commands marked "server" are run by a person (or an AI assistant that has a shell on
the machine hosting the stack). It never shows a password or key; the steps say where those are.
Keep it in line with README section 4 and the docs/*.md pages; tests/test_interface.py checks that every ./fleet
command written here is a real command.
"""
from interface import fleetlink
from interface.registry import FeatureError, feature

SECRET_NOTE = ("The password and key are in .env on the server, and `./fleet add` prints them for the person at the "
               "terminal. This interface never shows them.")
VERIFY = {"where": "anywhere", "action": "Check that it arrived: call get_device with the device_id. ONLINE means it works. "
                                         "A new device can take about 15 seconds to appear."}

KINDS = {
    "agent": {
        "title": "A Linux machine you can log in to (Raspberry Pi, laptop, server): the device agent",
        "use_when": "The device runs Linux with Python 3.9+ and you can install software on it.",
        "reports": "CPU, memory, disk, temperature (where it has a sensor) and uptime, every 5 seconds.",
        "steps": [
            {"where": "server", "action": "Register the device. It prints the exact commands for the device, with this "
                                           "server's address filled in, then waits for it.",
             "command": "./fleet add <device_id>"},
            {"where": "server", "action": "Copy the agent to the device (replace USER and DEVICE).",
             "command": "scp agent/device_agent.py agent/requirements.txt agent/install-service.sh USER@DEVICE:"},
            {"where": "device", "action": "Try it first; it stops when the terminal closes.",
             "command": "python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && "
                        "python device_agent.py --device-id <device_id> --endpoint <server_address>:4317"},
            {"where": "device", "action": "Keep it running for good: starts at boot, restarts if it crashes. Undo with "
                                           "`sudo bash install-service.sh --uninstall`.",
             "command": "sudo bash install-service.sh --device-id <device_id> --endpoint <server_address>:4317"},
            VERIFY,
        ],
        "limits": ["The device must be able to reach the server on port 4317.",
                   "A Raspberry Pi Zero 2 W follows the same steps; it has not been tested on one."],
        "docs": "README.md section 4a",
    },
    "mqtt": {
        "title": "A board that speaks MQTT (ESP32, Pico W, Arduino with Wi-Fi or Ethernet)",
        "use_when": "A microcontroller or custom board that can publish MQTT messages but cannot run the agent.",
        "reports": "Whatever the board publishes: uptime (required), how often it reports, temperature and other readings.",
        "steps": [
            {"where": "server", "action": "Turn the MQTT input on, once. It starts a broker and a bridge and remembers the "
                                           "choice. (To read a broker you already run: ./fleet up --mqtt-broker HOST --mqtt-user NAME.)",
             "command": "./fleet up --mqtt"},
            {"where": "server", "action": "Register the board. It prints the broker address, user name, password, topics and "
                                           "test commands, then waits for the board.",
             "command": "./fleet add <device_id> --mqtt"},
            {"where": "board", "action": "In the board's code, connect to that broker and publish plain numbers to "
                                          "fleet/<device_id>/uptime_seconds (required: any number that keeps growing, it is the "
                                          "\"alive\" signal), fleet/<device_id>/report_interval_seconds (how often it reports, "
                                          "in seconds) and fleet/<device_id>/temperature_celsius or any other reading."},
            VERIFY,
        ],
        "limits": [SECRET_NOTE,
                   "Payloads are plain numbers (21.5), not JSON or text. Plain MQTT, one shared password: keep it on a trusted network.",
                   "Tested only with simulated boards, not with a real ESP32, Pico or Arduino."],
        "docs": "docs/MQTT.md (examples for shell, Python, MicroPython and Arduino)",
    },
    "checkin": {
        "title": "Anything that can run curl: a cron job, a script, a router, a backup job",
        "use_when": "A job or device that can call a URL, including something behind a firewall that the server cannot reach.",
        "reports": "That it is alive each time it calls, plus any number you add as &name=value.",
        "steps": [
            {"where": "server", "action": "Register it (add --every 300 if it will call every 5 minutes; the default is 60 "
                                           "seconds). The first time this starts the check-in service and creates a key.",
             "command": "./fleet add <device_id> --checkin"},
            {"where": "device", "action": "Run this whenever it should report in; quote the address because it contains &. "
                                           "`every` must be how often it really calls: it is OFFLINE after 3 times that, never "
                                           "before 30 s. Put it as the last line of a script to mean \"the job finished\".",
             "command": "curl -fsS -m 10 \"http://<server_address>:<checkin_port>/checkin/<device_id>?key=<CHECKIN_KEY>&every=60\""},
            VERIFY,
        ],
        "limits": [SECRET_NOTE, "Plain HTTP with one shared key: keep it on a trusted network.",
                   "`every` is 5 to 28800 seconds; a daily job cannot be shown this way."],
        "docs": "docs/CHECKIN.md",
    },
    "watch": {
        "title": "Something you cannot install anything on (router, NAS, web service, a board running ESPHome): watch it",
        "use_when": "The server can reach the thing by its address, and nothing can be installed on it.",
        "reports": "Whether it answers a ping, accepts a connection on a port, serves a web page, or serves Prometheus metrics "
                   "(then its numbers are stored too).",
        "steps": [
            {"where": "server", "action": "Watch it. The kind is ping, tcp, http or metrics; use IP addresses, not .local names. "
                                           "It waits for the first answer and says why if there is none.",
             "command": "./fleet watch add <device_id> ping <address>"},
            {"where": "server", "action": "See what is watched and how it is doing.", "command": "./fleet watch list"},
            VERIFY,
        ],
        "limits": ["Examples: `./fleet watch add nas tcp 192.168.1.20:445`, `./fleet watch add pihole http http://192.168.1.2/admin/`."],
        "docs": "docs/WATCH.md",
    },
}


def fill(text, device_id, address, checkin_port):
    """Put the real values into a command; leave a placeholder where nothing is known."""
    return (text.replace("<device_id>", device_id or "<device_id>")
                .replace("<server_address>", address or "SERVER-ADDRESS")
                .replace("<checkin_port>", str(checkin_port)))


@feature
def how_to_add_device(kind: str = "", device_id: str = "") -> dict:
    """The steps to add a device, for one of four ways: "agent", "mqtt", "checkin" or "watch".

    kind: leave empty to list the four ways and when each fits; or name one for its exact steps.
    device_id: optional. If given, the commands come filled in with it (lowercase letters, digits, '.', '_', '-').
    Steps marked "server" are run on the machine that hosts the stack by a person or an assistant with a shell there;
    this tool only explains, and cannot run anything. Passwords and keys are never shown."""
    rule = fleetlink.device_id_rule()
    if device_id and not fleetlink.fleet().is_valid_device_id(device_id):
        raise FeatureError(f"device_id must be {rule['plain']}")
    if not kind:
        return {"ways": {k: {"title": v["title"], "use_when": v["use_when"]} for k, v in KINDS.items()},
                "device_id_rule": rule["plain"],
                "next": "Call again with kind set to one of these for the exact steps. You can mix all four ways."}
    guide = KINDS.get(kind)
    if guide is None:
        raise FeatureError(f"kind must be one of: {', '.join(KINDS)}")
    state = fleetlink.stack()
    address = fleetlink.server_address()
    port = state["ports"]["checkin"]
    steps = [{k: (fill(v, device_id, address, port) if k in ("command", "action") else v) for k, v in step.items()}
             for step in guide["steps"]]
    result = {"kind": kind, "title": guide["title"], "use_when": guide["use_when"], "reports": guide["reports"],
              "server_address": address, "device_id": device_id or None, "steps": steps, "limits": guide["limits"],
              "docs": guide["docs"], "device_id_rule": rule["plain"]}
    if kind == "mqtt":
        result["mqtt_is_on"] = state["mqtt"]["mode"] is not None
    if kind == "checkin":
        result["checkin_is_on"] = state["checkin"]["enabled"]
    return result
