#!/usr/bin/env python3
"""MQTT bridge: turns messages on  fleet/<device_id>/<metric>  into the device_* metrics the Device Fleet
dashboard and the DeviceOffline alert already use, so a board that can speak MQTT is a first-class device.

    fleet/garage-esp32/uptime_seconds            ->  device_uptime_seconds{device_id="garage-esp32"}
    fleet/garage-esp32/temperature_celsius       ->  device_temperature_celsius{device_id="garage-esp32"}
    fleet/garage-esp32/report_interval_seconds   ->  how often it reports (sets its OFFLINE limit)

The payload is a plain number such as 21.5. Samples are stamped when they ARRIVE here, because hobby boards
often have no real clock. Names follow docs/CONTRACT.md. Needs paho-mqtt (only for the live connection).

    MQTT_HOST=broker.lan MQTT_PASSWORD=... python bridge/mqtt_bridge.py --username fleet
"""
import argparse
import logging
import math
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("mqtt-bridge")

PREFIX = "fleet"
JOB = "mqtt-bridge"
# One rule in two places (agent/device_agent.py has the same pattern); tests/test_mqtt_bridge.py keeps them equal.
DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
NUMBER_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
UPTIME = "uptime_seconds"            # the liveness metric: without it a device is invisible (docs/CONTRACT.md)
INTERVAL = "report_interval_seconds"  # a device may declare how often it reports
MAX_INTERVAL = 86400                  # a day: devices silent for longer drop out of every view anyway
MAX_PAYLOAD = 64                      # bytes; a number never needs more
MAX_DEVICES = 500                     # an open broker must not be able to create unlimited series
MAX_METRICS_PER_DEVICE = 64
MAX_BUFFER = 20000                    # lines kept while VictoriaMetrics is unreachable; the oldest are dropped
MAX_WARNINGS = 200                    # distinct "log once" keys remembered
NO_UPTIME_GRACE = 30                  # seconds a device may publish without uptime_seconds before we say so


def parse_topic(topic):
    """'fleet/kitchen-pi/temperature_celsius' -> ('kitchen-pi', 'temperature_celsius'); None if it is not ours."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    device, metric = parts[1], parts[2]
    if DEVICE_ID_RE.fullmatch(device) and METRIC_RE.fullmatch(metric):
        return device, metric
    return None


def parse_value(payload):
    """A plain decimal number from the payload bytes; None for anything else (text, NaN, inf, too long)."""
    if len(payload) > MAX_PAYLOAD:
        return None
    try:
        text = payload.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not NUMBER_RE.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


class Bridge:
    """Turns (topic, payload) messages into Prometheus text lines and posts them to VictoriaMetrics.
    Thread safe: paho calls handle() from its network thread while flush() runs from another."""

    def __init__(self, vm_url, clock=time.time):
        self.vm_url = vm_url.rstrip("/")
        self.clock = clock
        self.lock = threading.Lock()
        self.pending = []          # Prometheus text lines not yet delivered
        self.metrics = {}          # device_id -> set of metric names seen
        self.interval = {}         # device_id -> declared report interval, re-sent with every uptime sample
        self.first_seen = {}       # device_id -> when its first message arrived
        self.has_uptime = set()
        self.warned = set()
        self.stats = {"accepted": 0, "invalid_topic": 0, "invalid_value": 0, "retained_ignored": 0, "limit": 0,
                      "delivered": 0, "dropped_unsent": 0}
        self.vm_ok = True
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a local service: no proxy

    # -------------------------------------------------------------- incoming messages
    def warn_once(self, key, message, *args):
        if key in self.warned or len(self.warned) >= MAX_WARNINGS:
            return
        self.warned.add(key)
        log.warning(message, *args)

    def handle(self, topic, payload, retained=False):
        """One MQTT message. Never raises: a bad message from a device must not stop the bridge."""
        parsed = parse_topic(topic)
        if parsed is None:
            self.stats["invalid_topic"] += 1
            self.warn_once(("topic", topic), "ignored topic %r: use %s/<device_id>/<metric> (lowercase letters, digits, "
                           "'.', '_' and '-' for the id; lowercase letters, digits and '_' for the metric)",
                           topic[:80], PREFIX)
            return
        device, metric = parsed
        value = parse_value(payload)
        if value is None:
            self.stats["invalid_value"] += 1
            self.warn_once(("value", device, metric), "ignored %s: the payload must be a plain number such as 21.5 "
                           "(Last Will and status texts are not used)", topic)
            return
        if metric == INTERVAL and not 0 < value <= MAX_INTERVAL:
            self.stats["invalid_value"] += 1
            self.warn_once(("interval", device), "ignored %s: report_interval_seconds must be between 0 and %d",
                           topic, MAX_INTERVAL)
            return
        with self.lock:
            if metric == INTERVAL:
                # Configuration, not a reading: remember it even when the broker replays it as a retained message.
                if device not in self.interval and len(self.interval) >= MAX_DEVICES:
                    self.stats["limit"] += 1
                    self.warn_once("max-devices", "more than %d devices: new device ids are ignored", MAX_DEVICES)
                    return
                self.interval[device] = value
                if retained:
                    return  # replayed config says nothing about whether the device is alive now
            elif retained:
                # The broker replays retained messages when we reconnect. They are old: never treat them as fresh,
                # or a dead device would look alive again after every bridge restart.
                self.stats["retained_ignored"] += 1
                return
            known = self.metrics.get(device)
            if known is None and len(self.metrics) >= MAX_DEVICES:
                self.stats["limit"] += 1
                self.warn_once("max-devices", "more than %d devices: new device ids are ignored", MAX_DEVICES)
                return
            if known is not None and metric not in known and len(known) >= MAX_METRICS_PER_DEVICE:
                self.stats["limit"] += 1
                self.warn_once(("max-metrics", device), "%s has %d metrics already: new ones are ignored",
                               device, MAX_METRICS_PER_DEVICE)
                return
            now = self.clock()
            self.metrics.setdefault(device, set()).add(metric)
            self.first_seen.setdefault(device, now)
            self.stats["accepted"] += 1
            stamp = int(now * 1000)
            if metric != INTERVAL:
                self.pending.append(self._line(device, metric, value, stamp))
            if metric == UPTIME:
                self.has_uptime.add(device)
                if device in self.interval:
                    self.pending.append(self._line(device, INTERVAL, self.interval[device], stamp))

    @staticmethod
    def _line(device, metric, value, stamp_ms):
        return f'device_{metric}{{device_id="{device}",job="{JOB}"}} {value!r} {stamp_ms}'

    def check_uptime(self):
        """Say, once, which devices publish readings but never uptime_seconds (so they would stay invisible)."""
        now = self.clock()
        with self.lock:
            missing = [d for d, t in self.first_seen.items() if d not in self.has_uptime and now - t > NO_UPTIME_GRACE]
        for device in missing:
            self.warn_once(("uptime", device),
                           "%s publishes readings but never %s/%s/%s, so it does not show up in the fleet table or "
                           "the offline alert. Publish its uptime in seconds (any number that grows) every time it reports.",
                           device, PREFIX, device, UPTIME)

    # -------------------------------------------------------------- delivery to VictoriaMetrics
    def flush(self):
        """Post everything pending. On failure the lines are kept (oldest dropped past MAX_BUFFER) and the error raised."""
        with self.lock:
            lines, self.pending = self.pending, []
        if not lines:
            return 0
        body = ("\n".join(lines) + "\n").encode()
        request = urllib.request.Request(self.vm_url + "/api/v1/import/prometheus", data=body, method="POST")
        try:
            with self._opener.open(request, timeout=10) as response:
                response.read()
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # an error response keeps its connection open until closed
            with self.lock:
                self.pending = lines + self.pending
                overflow = len(self.pending) - MAX_BUFFER
                if overflow > 0:
                    del self.pending[:overflow]
                    self.stats["dropped_unsent"] += overflow
            raise
        self.stats["delivered"] += len(lines)
        return len(lines)

    def flush_logged(self):
        """flush(), logging only when the state changes (down once, back once), never every second."""
        try:
            sent = self.flush()
        except (urllib.error.URLError, OSError) as exc:
            if self.vm_ok:
                log.error("cannot reach VictoriaMetrics at %s (%s); keeping readings and retrying", self.vm_url, exc)
            self.vm_ok = False
            return 0
        if not self.vm_ok:
            log.info("VictoriaMetrics is reachable again")
            self.vm_ok = True
        return sent


# ------------------------------------------------------------------ the live connection
def parse_config(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Bridge MQTT messages on fleet/<device_id>/<metric> into VictoriaMetrics.")
    ap.add_argument("--host", help="MQTT broker host (env MQTT_HOST)")
    ap.add_argument("--port", type=int, help="MQTT broker port (env MQTT_PORT, default 1883)")
    ap.add_argument("--username", help="MQTT user name (env MQTT_USERNAME). The password is only read from env MQTT_PASSWORD")
    ap.add_argument("--vm-url", help="VictoriaMetrics address (env VM_URL, default http://victoriametrics:8428)")
    a = ap.parse_args(argv)
    host = a.host or env.get("MQTT_HOST")
    if not host:
        ap.error("the broker address is required (--host or MQTT_HOST)")
    port = a.port if a.port is not None else int(env.get("MQTT_PORT") or 1883)
    if not 0 < port < 65536:
        ap.error("invalid port")
    return argparse.Namespace(host=host, port=port, username=a.username or env.get("MQTT_USERNAME") or None,
                              password=env.get("MQTT_PASSWORD") or None,
                              vm_url=a.vm_url or env.get("VM_URL") or "http://victoriametrics:8428")


def run(cfg):
    import paho.mqtt.client as mqtt  # imported here so the parsing above works (and is tested) without it

    bridge = Bridge(cfg.vm_url)
    stop = threading.Event()
    # A fixed client id on purpose: a second bridge started by mistake takes over instead of duplicating every sample.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fleet-mqtt-bridge", clean_session=True)
    if cfg.username:
        client.username_pw_set(cfg.username, cfg.password)
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    state = {"connected": None}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("the broker at %s:%s refused the connection: %s%s", cfg.host, cfg.port, reason_code,
                      " (check MQTT_USERNAME and MQTT_PASSWORD)" if "uthorized" in str(reason_code) else "")
            return
        client.subscribe(f"{PREFIX}/+/+", qos=0)
        state["connected"] = True
        log.info("connected to %s:%s; listening on %s/+/+", cfg.host, cfg.port, PREFIX)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        if state["connected"] is not False and not stop.is_set():
            log.warning("lost the connection to the broker (%s); reconnecting", reason_code)
        state["connected"] = False

    def on_connect_fail(client, userdata):  # paho says nothing by itself when the broker is simply not there
        if state["connected"] is not False:
            log.warning("cannot reach the broker at %s:%s; retrying every few seconds", cfg.host, cfg.port)
        state["connected"] = False

    def on_message(client, userdata, msg):
        try:
            bridge.handle(msg.topic, msg.payload, retained=bool(msg.retain))
        except Exception:  # one odd message must never stop the bridge
            log.exception("a message could not be processed")

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.on_connect_fail = on_connect_fail
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("starting: broker %s:%s, VictoriaMetrics %s", cfg.host, cfg.port, cfg.vm_url)
    client.connect_async(cfg.host, cfg.port, keepalive=30)
    client.loop_start()
    last_report = time.time()
    while not stop.wait(1.0):
        bridge.flush_logged()
        bridge.check_uptime()
        if time.time() - last_report >= 3600:
            last_report = time.time()
            log.info("last hour: %s", ", ".join(f"{k}={v}" for k, v in bridge.stats.items()))
    client.disconnect()
    client.loop_stop()
    bridge.flush_logged()
    log.info("stopped")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(parse_config(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
