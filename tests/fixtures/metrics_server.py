#!/usr/bin/env python3
"""A tiny web server for trying the watcher (scripts/validate_watch.sh runs it in a container).

  /          a web page (HTTP 200)
  /metrics   an ESPHome-style Prometheus answer: 4 series
  /hostile   what the watcher must not trust: a metric named like ours, NaN, and 2500 series
  anything else: 404

    python tests/fixtures/metrics_server.py [port]      (default 8000)
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ESPHOME = """\
# HELP esphome_sensor_value Value of sensor
# TYPE esphome_sensor_value gauge
esphome_sensor_value{id="temperature",name="Temperature",unit="C"} 21.5
esphome_sensor_value{id="humidity",name="Humidity",unit="%"} 48.25
esphome_sensor_failed{id="temperature",name="Temperature"} 0
esphome_binary_sensor_value{id="door",name="Door"} 1
"""


def hostile():
    lines = ["device_uptime_seconds 999999", "device_probe_success 0", "not_a_number NaN", "infinite +Inf"]
    lines += [f'hostile_metric_{i} {i}' for i in range(2500)]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {"/": ("text/html", "<html><body><h1>fixture</h1></body></html>"),
                  "/metrics": ("text/plain; version=0.0.4", ESPHOME),
                  "/hostile": ("text/plain; version=0.0.4", hostile())}
        ctype, body = routes.get(self.path.split("?")[0], ("text/plain", "not found"))
        data = body.encode()
        self.send_response(200 if self.path.split("?")[0] in routes else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8000), Handler).serve_forever()
