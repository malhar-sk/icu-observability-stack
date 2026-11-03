#!/usr/bin/env python3
"""A pretend device for trying the MQTT input, and the shape real board code should have.

It declares how often it reports, then publishes uptime (required), a made-up temperature and humidity:

    MQTT_PASSWORD=... python examples/mqtt_device.py --host 192.168.1.20 --user fleet --device-id demo-board

Needs: pip install paho-mqtt   (the password is read from the environment, never from the command line)
Stop it with Ctrl+C and the device goes OFFLINE after 3x its interval (at least 30 s).
"""
import argparse
import math
import os
import random
import sys
import threading
import time


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="the MQTT broker (from ./fleet add <id> --mqtt)")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user", help="broker user name; the password comes from the MQTT_PASSWORD environment variable")
    ap.add_argument("--device-id", default="demo-board", help="lowercase letters, digits, '.', '_' and '-'")
    ap.add_argument("--interval", type=float, default=10, help="seconds between reports (default 10)")
    ap.add_argument("--count", type=int, default=0, help="stop after this many reports (default: run until Ctrl+C)")
    ap.add_argument("--no-interval", action="store_true", help="do not declare an interval (the 30 s rule then applies)")
    a = ap.parse_args(argv)
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt is not installed: pip install paho-mqtt")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"device-{a.device_id}")
    if a.user:
        client.username_pw_set(a.user, os.environ.get("MQTT_PASSWORD", ""))
    answered = threading.Event()
    outcome = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        outcome["code"] = reason_code  # the broker's answer arrives after the TCP connection is already open
        answered.set()

    client.on_connect = on_connect
    try:
        client.connect(a.host, a.port, keepalive=max(30, int(a.interval * 3)))
    except OSError as exc:
        sys.exit(f"cannot reach the broker at {a.host}:{a.port}: {exc}")
    client.loop_start()
    if not answered.wait(10):
        sys.exit(f"the broker at {a.host}:{a.port} did not answer within 10 s")
    if outcome["code"].is_failure:
        sys.exit(f"the broker refused the login: {outcome['code']} (check --user and MQTT_PASSWORD)")
    topic = f"fleet/{a.device_id}"
    started = time.time()
    if not a.no_interval:  # retained: the broker keeps it, so it survives a bridge restart
        client.publish(f"{topic}/report_interval_seconds", f"{a.interval:g}", retain=True).wait_for_publish(5)
    sent = 0
    try:
        while True:
            now = time.time()
            temperature = 21 + 3 * math.sin(now / 60) + random.uniform(-0.2, 0.2)
            client.publish(f"{topic}/uptime_seconds", f"{now - started:.0f}")
            client.publish(f"{topic}/temperature_celsius", f"{temperature:.2f}")
            info = client.publish(f"{topic}/humidity_percent", f"{48 + random.uniform(-2, 2):.1f}")
            info.wait_for_publish(5)
            sent += 1
            print(f"report {sent}: uptime {now - started:.0f}s, {temperature:.2f} C")
            if a.count and sent >= a.count:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()
    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
