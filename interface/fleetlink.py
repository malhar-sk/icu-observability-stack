"""The adapter: the only code in interface/ that talks to ./fleet and the stack.

Features call the small functions here and never import ./fleet themselves. That keeps one rule for what "online"
means (the one ./fleet, the dashboard and the alert already share), and one place to look when ./fleet changes.
Everything here only reads. Python 3.9+, standard library only.
"""
import importlib.machinery
import importlib.util
import os
import threading

from interface import registry
from interface.registry import FeatureError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_WORDS = ("PASSWORD", "SECRET", "TOKEN", "KEY")  # a .env entry whose name has one of these holds a secret
SECRET_ENV_VARS = ("FLEET_API_TOKEN", "FLEET_GRAFANA_AUTH", "FLEET_MQTT_PASSWORD")
_module = None
_load_lock = threading.Lock()  # the API answers on several threads; load ./fleet once


def fleet():
    """./fleet, loaded by path (it has no .py extension): the very file the command line runs."""
    global _module
    with _load_lock:
        if _module is None:
            loader = importlib.machinery.SourceFileLoader("fleet_cli", os.path.join(ROOT, "fleet"))
            spec = importlib.util.spec_from_loader("fleet_cli", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            _module = module
        return _module


def _guard(fn):
    """Run fn; a problem ./fleet reports for the user becomes a FeatureError with the same words."""
    f = fleet()
    try:
        return fn(f)
    except f.FleetError as exc:
        raise FeatureError(str(exc), code=503) from None


def devices():
    """{device_id: info} as ./fleet list --json sees them. Raises FeatureError when the stack is not running."""
    def read(f):
        f.require_stack()
        return f.fetch_devices()
    return _guard(read)


def waiting(reporting):
    """({device_id: {"device_id", "kind", "added"}}, warning): the devices a person registered with ./fleet add or watches
    that are not in `reporting`. A list that cannot be read is a warning, not an error: the rest should still answer."""
    f = fleet()
    try:
        return f.waiting_devices(reporting), None
    except f.FleetError as exc:
        return {}, str(exc)


def stack():
    """Is each part of the server up, and which optional inputs are on? Asks the services directly; needs no Docker."""
    def read(f):
        env = f.read_env()
        vm, grafana = f.vm_healthy(), f.grafana_healthy()
        collector = f.port_open("localhost", f.OTLP_PORT)
        mqtt = f.mqtt_mode(env)
        checkin_port = f.checkin_port(env)
        try:
            targets = len(f.read_targets())
        except f.FleetError:
            targets = None
        return {
            "services": {"victoriametrics": vm, "grafana": grafana, "otel-collector": collector},
            "dashboard": f.dashboard_state() if grafana else None,
            "notifications": f.notify_configured() if grafana else None,
            "mqtt": {"mode": mqtt, "broker_reachable": f.port_open("localhost", f.MQTT_PORT) if mqtt == "bundled" else None},
            "checkin": {"enabled": f.checkin_enabled(env), "port": checkin_port,
                        "reachable": f.port_open("localhost", checkin_port) if f.checkin_enabled(env) else None},
            "watch": {"enabled": f.watch_enabled(env), "targets": targets},
            "ports": {"dashboard": 3000, "device_agent_endpoint": f.OTLP_PORT, "mqtt": f.MQTT_PORT, "checkin": checkin_port},
        }
    return _guard(read)


def mqtt_endpoint():
    """Where a board should connect: {"mode", "host", "port", "user"} (host is None when it is not known). Never the password."""
    f = fleet()
    env = f.read_env()
    mode = f.mqtt_mode(env)
    if mode == "external":
        return {"mode": mode, "host": env.get("MQTT_HOST") or None, "port": int(env.get("MQTT_PORT") or f.MQTT_PORT),
                "user": env.get("MQTT_USERNAME") or None}
    return {"mode": mode, "host": f.lan_ip() if mode == "bundled" else None, "port": f.MQTT_PORT,
            "user": env.get("MQTT_USERNAME", "fleet") if mode == "bundled" else None}


def server_address():
    """This machine's address as devices on the network see it, or None."""
    return fleet().lan_ip()


def device_id_rule():
    f = fleet()
    return {"pattern": f.DEVICE_ID_RE.pattern,
            "plain": "lowercase letters, digits, '.', '_' and '-'; up to 63 characters; starting with a letter or digit"}


def secret_values():
    """Every secret currently in use: the entries of .env that hold one, and the secret environment variables."""
    found = set()
    try:
        for name, value in fleet().read_env().items():
            if any(word in name.upper() for word in SECRET_WORDS):
                found.add(value)
    except Exception:  # a broken .env must not stop a check that protects it
        pass
    found.update(os.environ.get(n, "") for n in SECRET_ENV_VARS)
    return found


registry.register_secret_source(secret_values)
