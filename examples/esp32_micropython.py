"""Report to the fleet over MQTT. For an ESP32, a Pico W or another board running MicroPython.

1. Fill in the first block below (the Wi-Fi, and the MQTT password from .env on the server: MQTT_PASSWORD).
2. Replace read_temperature() with your sensor, or leave it and only the "alive" signal is sent.
3. Save this file on the board as main.py (with Thonny or mpremote) and restart it.

It reports every INTERVAL seconds: "I am alive" (uptime_seconds, required), how often it reports, and the temperature.
If the Wi-Fi or the broker goes away it reconnects, and after FAILURES_BEFORE_RESET failed rounds in a row it reboots.
Plain MQTT, no encryption: keep it on a network you trust. docs/MQTT.md explains the topics.

NOT run on a real board. Its logic was run against stand-ins for MicroPython's libraries (tests/test_firmware.py).
"""
import time

import machine
import network
from umqtt.simple import MQTTClient

WIFI_SSID = "your-wifi"
WIFI_PASSWORD = "your-wifi-password"
DEVICE = "REPLACE-DEVICE-ID"
BROKER = "REPLACE-BROKER-ADDRESS"
PORT = 1883
USER = "REPLACE-MQTT-USER"
PASSWORD = "THE-PASSWORD"
INTERVAL = 60                # seconds between reports; the server says OFFLINE after 3 times this (never under 30 s)
FAILURES_BEFORE_RESET = 10   # failed rounds in a row (5 s apart) before the board reboots itself


def read_temperature():
    """Degrees Celsius from your sensor, or None when there is no reading."""
    return None


def topic(metric):
    return ("fleet/%s/%s" % (DEVICE, metric)).encode()


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(30):
            if wlan.isconnected():
                break
            time.sleep(1)
    if not wlan.isconnected():
        raise OSError("no Wi-Fi")


def connect_broker():
    client = MQTTClient(DEVICE, BROKER, port=PORT, user=USER, password=PASSWORD, keepalive=INTERVAL * 2)
    client.connect()
    client.publish(topic("report_interval_seconds"), str(INTERVAL).encode(), retain=True)
    return client


def main():
    client = None
    failures = 0
    uptime_ms = 0                     # counted here, so it starts from 0 on every boot and never wraps
    last = time.ticks_ms()
    while True:
        try:
            connect_wifi()
            if client is None:
                client = connect_broker()
            now = time.ticks_ms()
            uptime_ms += time.ticks_diff(now, last)
            last = now
            client.publish(topic("uptime_seconds"), str(uptime_ms // 1000).encode())
            temperature = read_temperature()
            if temperature is not None:
                client.publish(topic("temperature_celsius"), ("%.1f" % temperature).encode())
            failures = 0
            time.sleep(INTERVAL)
        except OSError:               # Wi-Fi or broker trouble: start the connection again
            client = None
            failures += 1
            if failures >= FAILURES_BEFORE_RESET:
                machine.reset()
            time.sleep(5)


if __name__ == "__main__":
    main()
