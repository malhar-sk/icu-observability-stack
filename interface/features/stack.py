"""A question about the server itself: is it up, and which optional inputs are on?"""
from interface import fleetlink
from interface.registry import FeatureError, feature

DASHBOARD_MEANING = {
    "ok": "the Device Fleet dashboard is in place",
    "login": "Grafana refused the login stored in .env (the password was probably changed in Grafana)",
    "missing": "Grafana is up but the Device Fleet dashboard is not provisioned",
    "unreachable": "Grafana did not answer",
}


@feature
def stack_status() -> dict:
    """Is the monitoring server healthy, which optional inputs are turned on, and how many devices are online?

    Asks the services directly, so it also works (and says so) when the stack is down. Use it first when setting
    things up: it shows whether MQTT, the check-in URL and watching are on, and the ports and address devices use."""
    s = fleetlink.stack()
    running = all(s["services"].values())
    address = fleetlink.server_address()
    try:
        found = fleetlink.devices() if s["services"]["victoriametrics"] else None
    except FeatureError:
        found = None
    devices = None if found is None else {
        "online": sum(1 for d in found.values() if d["state"] == "ONLINE"),
        "offline": sum(1 for d in found.values() if d["state"] != "ONLINE"),
        "waiting": len(fleetlink.waiting(found)[0]),
    }
    result = {
        "running": running,
        "services": s["services"],
        "server_address": address,
        "dashboard": {"url": f"http://{address or 'localhost'}:{s['ports']['dashboard']}", "state": s["dashboard"],
                      "meaning": DASHBOARD_MEANING.get(s["dashboard"])},
        "alerts_go_to_a_notification_channel": s["notifications"],
        "inputs": {
            "device_agent": {"on": True, "endpoint": f"{address or 'SERVER-ADDRESS'}:{s['ports']['device_agent_endpoint']}"},
            "mqtt": {"on": s["mqtt"]["mode"] is not None, "broker": s["mqtt"]["mode"], "port": s["ports"]["mqtt"],
                     "broker_reachable": s["mqtt"]["broker_reachable"], "turn_on": "./fleet up --mqtt"},
            "checkin_url": {"on": s["checkin"]["enabled"], "port": s["ports"]["checkin"], "reachable": s["checkin"]["reachable"],
                            "turn_on": "./fleet add <device_id> --checkin (starts it the first time; ./fleet up --no-checkin turns it off)"},
            "watch": {"on": s["watch"]["enabled"], "targets": s["watch"]["targets"],
                      "turn_on": "./fleet watch add <name> <ping|tcp|http|metrics> <address>"},
        },
        "devices": devices,
    }
    if not running:
        down = [name for name, ok in s["services"].items() if not ok]
        result["next_step"] = (f"Not running or not answering: {', '.join(down)}. A person starts it with `./fleet up` "
                               "on the machine that hosts the stack (this interface cannot).")
    return result
