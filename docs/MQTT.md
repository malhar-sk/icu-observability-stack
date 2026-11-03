# Boards that speak MQTT

For boards and gadgets that cannot run the Linux agent (ESP32, Pico W, Arduino with Wi-Fi or Ethernet, microcontroller
projects, custom hardware) but can publish MQTT messages. They show up in the same **Device Fleet** dashboard, with the
same ONLINE/OFFLINE state and the same `DeviceOffline` alert as any other device.

```
board --MQTT--> broker (Mosquitto) --> mqtt-bridge --> VictoriaMetrics <-- Grafana
```

Demonstration-grade, like the rest of this repo: plain MQTT (no encryption) and one shared password. Keep it on a
network you trust. Not for medical or safety use.

## Turn it on

```bash
./fleet up --mqtt                                        # our own broker (Mosquitto) and the bridge
./fleet up --mqtt-broker 192.168.1.20 --mqtt-user ha     # or a broker you already run (see below)
./fleet add garage-esp32 --mqtt                          # prints exactly what the board needs, then waits for it
./fleet up --no-mqtt                                     # turn it off again
```

The choice is remembered (in `.env`, which is private to you and not committed), so a later plain `./fleet up` keeps it.
If something else already uses port 1883 (often a broker you run for Home Assistant), `./fleet up --mqtt` stops before
changing anything and tells you to read that broker instead.

**Change the password** (for example after sharing it by mistake): delete the `MQTT_PASSWORD=` line from `.env`, run
`./fleet up --mqtt`, then give every board the new one (`./fleet add <id> --mqtt` prints it). The old one stops working
at once.

**A broker you already run** (for example the one Home Assistant uses): pass its address, not `localhost` (inside the
bridge's container that would be the container itself). The password is read from the `FLEET_MQTT_PASSWORD` environment
variable, or asked for with the input hidden; it is never taken from the command line. The bridge needs permission to
read `fleet/+/+`, and the broker must accept plain MQTT (TLS is not supported yet).

**What it costs** (measured on x86 with Docker 29): about 126 MB of disk (Mosquitto image 42 MB, bridge image 85 MB,
which includes a small Python) and about 18 MiB of RAM while running (broker 3 MiB, bridge 15 MiB). Starting everything
from nothing, with the images already downloaded, took 27 s.

## What a board publishes

Plain numbers on `fleet/<device_id>/<metric>`:

| Topic | Value | Notes |
|---|---|---|
| `fleet/garage-esp32/uptime_seconds` | any number that keeps growing, e.g. seconds since boot | **Required.** It is the "I am alive" signal. Without it the board never appears. |
| `fleet/garage-esp32/report_interval_seconds` | how often the board reports, in seconds | Optional but recommended for anything that reports slowly. Counts as OFFLINE after 3x this (never under 30 s). |
| `fleet/garage-esp32/temperature_celsius` | `21.5` | Shown in the dashboard's temperature column and graph. |
| `fleet/garage-esp32/humidity_percent`, `battery_volts`, ... | `48` | Any other name is accepted and stored as `device_<name>`; look at it in Grafana Explore. |

Rules the bridge enforces (it never crashes on a bad message; it ignores it and says why, once, in its log):

- The payload is a plain number: `21.5`, `-3`, `1e3`. Not `21.5C`, not JSON, not `"online"`.
- `device_id`: lowercase letters, digits, `.`, `_`, `-` (up to 63, starting with a letter or digit). The metric name:
  lowercase letters, digits and `_`, starting with a letter.
- **When it arrives is when it counts.** Readings are timestamped by the bridge, because hobby boards often have no
  real clock.
- **Retained messages are not readings.** The broker replays them whenever the bridge reconnects, so a dead board would
  look alive after every restart. The one exception is `report_interval_seconds`: publish it once with the retain flag
  and the bridge remembers it for the board, or publish it every time; either works.
- At most 500 devices and 64 metrics per device (an open broker must not be able to create endless series).

**How long a board may be quiet.** The offline limit is `max(30 s, 3 x report_interval_seconds)`. A board that reports
every minute and says so (`60`) is OFFLINE after 3 minutes; one that says nothing is held to 30 s. The Linux agent
declares its own interval the same way, so `--interval 60` works too. The exact rule is in `CONTRACT.md`.

## Examples

**Shell** (needs the mosquitto clients; `./fleet add <id> --mqtt` prints these with your address and password filled in).
Tested:

```bash
mosquitto_pub -h 192.168.1.10 -u fleet -P 'THE-PASSWORD' -t fleet/garage-esp32/report_interval_seconds -m 60 -r
mosquitto_pub -h 192.168.1.10 -u fleet -P 'THE-PASSWORD' -t fleet/garage-esp32/uptime_seconds -m 1
mosquitto_pub -h 192.168.1.10 -u fleet -P 'THE-PASSWORD' -t fleet/garage-esp32/temperature_celsius -m 21.5
```

**Python** on any computer or Linux board: [`examples/mqtt_device.py`](../examples/mqtt_device.py) is a pretend device
you can run as it is (`pip install paho-mqtt`), and the shape real code should have. It stops with a clear message if
the broker refuses the login. Tested.

**MicroPython** (ESP32, Pico W). *Not run on real hardware; check it on yours.*

```python
import time
from umqtt.simple import MQTTClient          # connect to Wi-Fi first, as usual

DEVICE, BROKER, USER, PASSWORD = "garage-esp32", "192.168.1.10", "fleet", "THE-PASSWORD"
def topic(metric):
    return ("fleet/%s/%s" % (DEVICE, metric)).encode()

client = MQTTClient(DEVICE, BROKER, user=USER, password=PASSWORD, keepalive=120)
client.connect()
client.publish(topic("report_interval_seconds"), b"60", retain=True)
uptime = 0
while True:
    client.publish(topic("uptime_seconds"), str(uptime).encode())
    client.publish(topic("temperature_celsius"), ("%.1f" % read_temperature()).encode())  # your sensor code
    uptime += 60
    time.sleep(60)
```

**Arduino IDE** (ESP32 or ESP8266, library PubSubClient). *Not compiled or run on real hardware; treat it as a
starting point.*

```cpp
#include <WiFi.h>                    // ESP8266: <ESP8266WiFi.h>
#include <PubSubClient.h>

const char* WIFI_SSID = "your-wifi";  const char* WIFI_PASS = "your-wifi-password";
const char* BROKER = "192.168.1.10";  const char* USER = "fleet";  const char* PASS = "THE-PASSWORD";
const char* DEVICE = "garage-esp32";

WiFiClient wifi;
PubSubClient mqtt(wifi);

void publishNumber(const char* metric, float value, bool retain = false) {
  char topic[80], payload[24];
  snprintf(topic, sizeof topic, "fleet/%s/%s", DEVICE, metric);
  snprintf(payload, sizeof payload, "%.2f", value);
  mqtt.publish(topic, payload, retain);
}

void setup() {
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  mqtt.setServer(BROKER, 1883);
  mqtt.setKeepAlive(120);
}

void loop() {
  while (!mqtt.connected()) {
    if (mqtt.connect(DEVICE, USER, PASS)) publishNumber("report_interval_seconds", 60, true);
    else delay(5000);
  }
  publishNumber("uptime_seconds", millis() / 1000.0);
  publishNumber("temperature_celsius", 21.5);            // your sensor reading
  mqtt.loop();
  delay(60000);
}
```

## When a board does not show up

`./fleet add <id> --mqtt` waits for it and, if it never appears, points you here. Read the bridge's log:

```bash
docker compose logs mqtt-bridge
```

| It says | It means |
|---|---|
| `refused the connection: Not authorized` | wrong user name or password in the board (or in `.env` for the bridge) |
| `cannot reach the broker at ...` | the broker is down, or the address is wrong |
| `ignored topic '...'` | the topic is not `fleet/<device_id>/<metric>` with legal names |
| `ignored fleet/...: the payload must be a plain number` | the payload is text, JSON, `nan` or too long |
| `<id> publishes readings but never fleet/<id>/uptime_seconds` | add the uptime topic: without it the board stays invisible |
| `cannot reach VictoriaMetrics ... keeping readings and retrying` | the database is down; up to 20,000 readings are kept and delivered, with their original arrival times, when it returns |

## Limits (on purpose, for now)

- **No Last Will / status topics.** A board is declared OFFLINE by silence (30 s at the least), not by the broker
  announcing its disconnect. Last Will only fires on a broken connection, not on a clean disconnect, and the broker
  notices a dead board only after about 1.5x its keep-alive, so the gain is small and it would have changed the alert
  rule everything else depends on. A `status` payload such as `online` is ignored (and logged once).
- **Plain MQTT only:** no TLS, and one shared user for all boards. Anyone on your network who has the password can
  publish as any device. The broker's access list restricts that user to `fleet/#`, so it cannot be used to read or
  write other topics on the bundled broker.
- **Our topic scheme only.** Devices that already publish their own topics (Tasmota, ESPHome, Zigbee2MQTT, Home
  Assistant) are not mapped yet; they would need a mapping layer.
- **Real boards are untested.** Everything was validated with simulated devices (`scripts/validate_mqtt.sh`), a real
  Mosquitto and the real stack. The broker and bridge also built and ran on a Raspberry Pi 5 (arm64), with a simulated board
  sent by `mosquitto_pub`. No ESP32, Pico or Arduino was available.
