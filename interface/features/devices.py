"""Questions about the devices: which are there, which are online, and how is one doing.

Read-only. The meaning of ONLINE/OFFLINE is not decided here: it comes from ./fleet, the same rule as the dashboard
and the alert (see docs/CONTRACT.md).
"""
import math
import time

from interface import fleetlink
from interface.registry import FeatureError, feature

VIA = {"agent": "the device agent", "mqtt": "MQTT", "checkin": "the check-in URL", "watcher": "being watched from the server"}
STATES = ("all", "online", "offline", "waiting")
KIND_VIA = {"watch": "watcher"}  # a registered device's "way" as ./fleet names it, to the name used for reports_via
KNOWN_LIMIT = 50  # how many device ids "not found" lists to help with a typo


def _number(value):
    """A reading as a float, or None. NaN and infinity are not valid JSON, so they become None. Always a float, so
    a field never changes type from one device to the next (30 and 180.0)."""
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)) if epoch else None


def _duration(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min" + (f" {s % 60} s" if s % 60 else "")
    if s < 86400:
        return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")
    return f"{s // 86400} d" + (f" {s % 86400 // 3600} h" if s % 86400 // 3600 else "")


def present(d):
    """One device as the interface shows it: units in the names, null where the device does not report a reading."""
    return {
        "device_id": d["device_id"],
        "hostname": d["hostname"] or None,
        "state": d["state"],
        "reports_via": d["via"],
        "last_seen": _iso(d["last_seen"]),
        "seconds_since_last_report": _number(d["age_seconds"]),
        "offline_after_seconds": _number(d["offline_after"]),
        "report_interval_seconds": _number(d["report_interval"]),
        "cpu_percent": _number(d["cpu"]),
        "memory_percent": _number(d["mem"]),
        "disk_percent": _number(d["disk"]),
        "temperature_celsius": _number(d["temp"]),
        "uptime_seconds": _number(d["uptime"]),
        "registered_at": None,
    }


def present_waiting(w):
    """A device that is expected (or watched) but has not reported: the same keys, nothing to show but when it was registered."""
    out = {k: None for k in present({"device_id": "", "hostname": "", "state": "", "via": "", "last_seen": None, "age_seconds": None,
                                      "offline_after": None, "report_interval": None, "cpu": None, "mem": None, "disk": None,
                                      "temp": None, "uptime": None})}
    out.update(device_id=w["device_id"], state="WAITING", reports_via=KIND_VIA.get(w["kind"], w["kind"]),
               registered_at=_iso(w.get("added")))
    return out


def explain_waiting(w):
    """Why a device is not shown yet and what finishes the setup."""
    since = f"Registered {_duration(time.time() - w['added'])} ago" if w.get("added") else "Being watched"
    asks = {
        "agent": ["Run the device agent on the device (how_to_add_device with kind=agent gives the exact commands) and make "
                  "sure it can reach the server on port 4317."],
        "mqtt": ["A brand-new board has no software to report with yet: flash it first (how_to_add_device with kind=mqtt "
                 "and firmware=micropython or firmware=arduino gives a ready sketch).",
                 f"It must publish fleet/{w['device_id']}/uptime_seconds. On the server, `docker compose logs mqtt-bridge` "
                 "shows what the bridge refused and why."],
        "checkin": ["Run the check-in curl command on the device once (how_to_add_device with kind=checkin gives it, "
                    "with this server's address)."],
        "watch": ["The server has not got an answer from what it watches. `./fleet watch list` on the server shows what it "
                  "tried; check the address, and that the target answers from the server's network."],
    }
    steps = list(asks.get(w["kind"], asks["agent"]))
    steps.append(f"If you no longer expect it, a person can stop with `./fleet remove {w['device_id']}` on the server "
                 "(this interface cannot do that).")
    return {"explanation": f"{since} and has not reported in the last 24 hours, so it has no state yet. A device appears here as "
                           "ONLINE or OFFLINE once it reports. No alert fires for a device that has never reported.",
            "next_steps": steps}

def explain(d):
    """What the state means for this device, in words, and what to try. Mirrors `./fleet check`."""
    limit = _duration(d["offline_after"])
    interval = d["report_interval"]
    why_limit = (f"3 x the {_duration(interval)} it reports every, never under 30 s" if interval
                 else "30 s, because it has not said how often it reports")
    if d["state"] == "ONLINE":
        return {"explanation": f"Reported {_duration(d['age_seconds'])} ago. It counts as OFFLINE after {limit} of silence "
                               f"({why_limit}).", "next_steps": []}
    steps = {
        "agent": ["Check the device has power and a network connection, and that the device agent is running on it."],
        "mqtt": [f"Check the board is publishing fleet/{d['device_id']}/uptime_seconds to the broker.",
                 "On the server, `docker compose logs mqtt-bridge` shows what the bridge refused and why."],
        "checkin": ["Check whatever should call the check-in URL is still running (a cron job, a script). "
                    "Running its curl command by hand without -s shows the server's answer."],
        "watcher": ["The server could not reach what it watches. `./fleet watch list` on the server shows what it tried."],
    }[d["via"]]
    steps.append(f"If the device is retired, a person can forget it with `./fleet remove {d['device_id']}` on the server "
                 "(this interface cannot do that).")
    return {"explanation": f"No report for {_duration(d['age_seconds'])}. The limit for this device is {limit} "
                           f"({why_limit}).", "next_steps": steps}


@feature
def list_devices(state: str = "all") -> dict:
    """List the devices that reported in the last 24 hours, with their state and latest readings.

    state: "all" (the default), "online", "offline" or "waiting". WAITING is a device a person registered with
    `./fleet add` (or a watched target) that has not reported yet, for example a brand-new board with no firmware: it
    has no readings, only when it was registered. Readings are null where a device does not report them (a check-in
    or MQTT device has no CPU, for example). Use get_device for one device in detail."""
    if state not in STATES:
        raise FeatureError(f"state must be one of: {', '.join(STATES)}")
    found = fleetlink.devices()
    waiting, warning = fleetlink.waiting(found)
    online = sum(1 for d in found.values() if d["state"] == "ONLINE")
    shown = [present(d) for d in found.values() if state in ("all", d["state"].lower())]
    shown += [present_waiting(w) for w in waiting.values() if state in ("all", "waiting")]
    out = {"count": len(shown), "online": online, "offline": len(found) - online, "waiting": len(waiting), "devices": shown}
    if not found and not waiting:
        out["note"] = "No device has reported in the last 24 hours. See how_to_add_device."
    if warning:
        out["warning"] = f"The list of expected devices could not be read, so waiting devices are not shown: {warning}"
    return out


@feature
def get_device(device_id: str) -> dict:
    """One device in detail: its state, why, and what to try. Also the way to check that a device you just set up arrived.

    device_id: the ID the device reports with. State is ONLINE or OFFLINE once it has reported, and WAITING when it was
    registered (`./fleet add`) but has not reported yet. An ID that is neither reporting nor registered is
    found=false (not an error), together with the IDs that are known. A device that was only just set up can take up
    to about 15 seconds to appear, so ask again if you have just started it."""
    found = fleetlink.devices()
    d = found.get(device_id)
    if d is not None:
        return {"found": True, **present(d), **explain(d)}
    waiting, _ = fleetlink.waiting(found)
    if device_id in waiting:
        return {"found": True, **present_waiting(waiting[device_id]), **explain_waiting(waiting[device_id])}
    known = sorted(set(found) | set(waiting))
    return {"found": False, "device_id": device_id,
            "explanation": "No device with this ID has reported in the last 24 hours, and none is registered under it. If "
                           "you have only just set it up, wait about 15 seconds and ask again. IDs are exact and lowercase.",
            "known_devices": known[:KNOWN_LIMIT]}
