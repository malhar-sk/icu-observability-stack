// Report to the fleet over MQTT. For an ESP32 in the Arduino IDE (board support "esp32", library "PubSubClient").
//
// 1. Fill in the first block below (the Wi-Fi, and the MQTT password from .env on the server: MQTT_PASSWORD).
// 2. Replace readTemperature() with your sensor, or leave it and only the "alive" signal is sent.
// 3. Pick your board, upload, and open the Serial Monitor at 115200 to watch it.
//
// It reports every INTERVAL_SECONDS: "I am alive" (uptime_seconds, required), how often it reports, and the temperature.
// If the Wi-Fi or the broker goes away it reconnects. Plain MQTT, no encryption: keep it on a network you trust.
// docs/MQTT.md explains the topics.
//
// NOT compiled with the Arduino toolchain and NOT run on a board. Only its syntax was checked (tests/test_firmware.py).

#include <WiFi.h>
#include <PubSubClient.h>

const char* WIFI_SSID = "your-wifi";
const char* WIFI_PASSWORD = "your-wifi-password";
const char* DEVICE = "REPLACE-DEVICE-ID";
const char* BROKER = "REPLACE-BROKER-ADDRESS";
const uint16_t PORT = 1883;
const char* MQTT_USER = "REPLACE-MQTT-USER";
const char* MQTT_PASSWORD = "THE-PASSWORD";
const unsigned long INTERVAL_SECONDS = 60;  // the server says OFFLINE after 3 times this (never under 30 s)

WiFiClient wifi;
PubSubClient mqtt(wifi);

float readTemperature() {
  return NAN;  // degrees Celsius from your sensor; NAN means "no reading"
}

void publishNumber(const char* metric, double value, bool retain = false) {
  char topic[96], payload[32];
  snprintf(topic, sizeof topic, "fleet/%s/%s", DEVICE, metric);
  snprintf(payload, sizeof payload, "%.2f", value);
  mqtt.publish(topic, payload, retain);
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) delay(500);
  if (WiFi.status() != WL_CONNECTED) ESP.restart();  // no Wi-Fi for 30 s: start again
}

bool connectBroker() {
  if (!mqtt.connect(DEVICE, MQTT_USER, MQTT_PASSWORD)) return false;
  publishNumber("report_interval_seconds", INTERVAL_SECONDS, true);  // retained: the server remembers it for this board
  return true;
}

void setup() {
  Serial.begin(115200);
  mqtt.setServer(BROKER, PORT);
  mqtt.setKeepAlive(INTERVAL_SECONDS * 2);
  connectWifi();
}

void loop() {
  connectWifi();
  if (!mqtt.connected() && !connectBroker()) {
    Serial.println("broker not reachable; trying again");
    delay(5000);
    return;
  }
  publishNumber("uptime_seconds", millis() / 1000.0);  // restarts from 0 about every 49 days: that looks like a reboot
  float t = readTemperature();
  if (!isnan(t)) publishNumber("temperature_celsius", t);
  mqtt.loop();
  delay(INTERVAL_SECONDS * 1000UL);
}
