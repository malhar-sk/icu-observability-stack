# The MQTT bridge as a small container (see docker-compose.yml, service "mqtt-bridge").
FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mqtt_bridge.py .
USER nobody
ENTRYPOINT ["python", "-u", "/app/mqtt_bridge.py"]
