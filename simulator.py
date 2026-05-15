#!/usr/bin/env python3
"""
SunEnergyXT 500 PRO — Device Simulator

Simulates the local HTTP API of the SunEnergyXT 500 Series device,
including mDNS/Zeroconf announcement so Home Assistant can discover it
automatically — just like the real device.

Usage:
    python simulator.py [--ip 0.0.0.0] [--port 80] [--sn TBsimulator0001]

Requirements:
    pip install flask zeroconf

Endpoints implemented:
    GET  /read   → returns current device state
    POST /write  → accepts partial state updates (GS, IS, SI, SA, etc.)

The simulator logs all /write calls to the console so you can verify
that the HA integration is writing GS correctly.
"""

import argparse
import json
import logging
import socket
import threading
import time
from datetime import datetime, UTC

from flask import Flask, jsonify, request
from zeroconf import ServiceInfo, Zeroconf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("simulator")

# ---------------------------------------------------------------------------
# Default device state — mirrors a real 500 PRO response from /read
# ---------------------------------------------------------------------------
DEFAULT_STATE: dict = {
    # Identity
    "SN": "TBsimulator0001",
    "PK": 2,                  # 2 = 500 Pro (2400W)
    "DevType": "SunEnergyXT500Pro",

    # System status
    "ST": 2,                  # 2 = Running
    "WT": 3,                  # Wi-Fi connected

    # PV input (simulated ~1500W solar)
    "PV": 1500,
    "PV1": 400, "PV2": 400, "PV3": 350, "PV4": 350,
    "II1": 1.8, "II2": 1.8, "II3": 1.6, "II4": 1.6,
    "VP1": 222.0, "VP2": 222.0, "VP3": 219.0, "VP4": 219.0,

    # Power flow
    "IW": 1500,               # total input
    "OP": 1200,               # total output
    "GP": 0,                  # grid port power (GS setpoint result)
    "LP": 0,                  # load port power
    "PB": 300,                # battery power (positive = charging)

    # Battery
    "SC": 62,                 # total SOC %
    "SC0": 62,                # head unit SOC

    # GS setpoint (writable)
    "GS": 0,
    "IS": 2400,
    "SI": 10,
    "SA": 95,
    "SO": 10,
    "PT": 1440,

    # Modes
    "LM": 1,                  # local mode on
    "MM": 0,                  # self-consumption mode off
    "PM": 0,

    # Meter
    "MS": 0,                  # no meter bound
    "MD": "",

    # Energy counters (Wh, raw)
    "PD": 4200,               # today PV
    "GD1": 0,                 # today grid charge
    "GD2": 580,               # today grid feed-in
    "LD": 0,

    # Network
    "IP": "127.0.0.1",
    "COM": 80,

    # Firmware
    "ES": "1.1.3",
    "AS": "1.0.6",
    "DS": "1.0.5",
    "BS0": "4.0.5",

    # Wi-Fi
    "WS": "SimulatorNet",
    "WR": -55,
}

# Fields that can be written via /write
WRITABLE_FIELDS = {
    "GS", "IS", "SI", "SA", "SO", "PT",
    "LM", "MM", "PM", "MD", "TZ", "RT",
    "UO", "UP", "UG", "FP", "NT",
}

# ---------------------------------------------------------------------------
# Shared mutable state (thread-safe via lock)
# ---------------------------------------------------------------------------
state = DEFAULT_STATE.copy()
state_lock = threading.Lock()
write_log: list[dict] = []

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.logger.setLevel(logging.WARNING)  # silence Flask request logs (we do our own)


@app.route("/read", methods=["GET"])
def read():
    with state_lock:
        snapshot = state.copy()
    snapshot["timestamp"] = int(datetime.now(UTC).timestamp() * 1000)
    return jsonify({"state": {"reported": snapshot}})


@app.route("/write", methods=["POST"])
def write():
    body = request.get_json(silent=True)
    if not body or "state" not in body:
        return jsonify({"error": "invalid body"}), 400

    updates = body["state"]
    applied = {}
    ignored = {}

    with state_lock:
        for key, value in updates.items():
            if key in WRITABLE_FIELDS:
                if key == "RT":
                    log.info("🔄  RESTART triggered (RT=1)")
                else:
                    old = state.get(key, "—")
                    state[key] = value
                    applied[key] = {"old": old, "new": value}
            else:
                ignored[key] = value

    entry = {
        "time": datetime.now(UTC).isoformat(),
        "applied": applied,
        "ignored": ignored,
    }
    write_log.append(entry)

    if applied:
        for k, v in applied.items():
            log.info("✏️   WRITE  %-4s  %s → %s", k, v["old"], v["new"])
    if ignored:
        log.warning("⚠️   IGNORED fields (not writable): %s", list(ignored.keys()))

    return jsonify({"result": "accepted", "applied": list(applied.keys())}), 200


@app.route("/sim/state", methods=["GET"])
def sim_state():
    """Extra endpoint: inspect full simulator state (not part of real device API)."""
    with state_lock:
        return jsonify(state)


@app.route("/sim/writes", methods=["GET"])
def sim_writes():
    """Extra endpoint: inspect all received /write calls."""
    return jsonify(write_log)


@app.route("/sim/set", methods=["POST"])
def sim_set():
    """
    Extra endpoint: inject arbitrary state changes into the simulator.
    Useful for simulating changing PV power, SOC, grid flow etc.

    Example:
        curl -X POST http://localhost:8500/sim/set \\
             -H 'Content-Type: application/json' \\
             -d '{"PV": 2000, "SC": 80, "GP": -500}'
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid body"}), 400
    with state_lock:
        for k, v in body.items():
            state[k] = v
            log.info("🎛️   SIM SET  %-4s = %s", k, v)
    return jsonify({"ok": True, "updated": list(body.keys())})


# ---------------------------------------------------------------------------
# mDNS / Zeroconf announcement
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    """Best-effort: find the LAN IP of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_mdns(sn: str, ip: str, port: int) -> Zeroconf:
    """
    Announce the simulator via mDNS so HA can discover it automatically.
    The hostname follows the pattern the integration expects:
        sunlit-{sn}.local
    """
    zeroconf = Zeroconf()
    hostname = f"sunlit-{sn}.local."

    info = ServiceInfo(
        type_="_http._tcp.local.",
        name=f"SunEnergyXT-{sn}._http._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={
            "model": "SunEnergyXT500Pro",
            "sn": sn,
            "version": "1.1.3",
        },
        server=hostname,
    )
    zeroconf.register_service(info)
    log.info("📡  mDNS announced as %s (%s:%d)", hostname, ip, port)
    return zeroconf


# ---------------------------------------------------------------------------
# SOC / PV simulation loop (optional background thread)
# ---------------------------------------------------------------------------
def simulate_dynamics():
    """
    Gently varies PV power and SOC over time so the dashboard looks alive.
    Runs as a daemon thread — safe to kill when the process exits.
    """
    import math
    t = 0
    while True:
        time.sleep(5)
        t += 5
        # Simulate a sine-wave solar day (peak at t=43200s = noon)
        hour_of_day = (t % 86400) / 3600
        solar_factor = max(0.0, math.sin(math.pi * hour_of_day / 12))
        pv = int(solar_factor * 2200)

        with state_lock:
            state["PV"] = pv
            state["PV1"] = pv // 4
            state["PV2"] = pv // 4
            state["PV3"] = pv // 4
            state["PV4"] = pv - 3 * (pv // 4)
            state["IW"] = pv

            # Charge battery if PV > GS target
            gs = state.get("GS", 0)
            surplus = pv - abs(gs) if gs <= 0 else pv - gs
            soc = state["SC"]
            if surplus > 50 and soc < state.get("SA", 95):
                state["SC"] = min(state.get("SA", 95), soc + 0.1)
                state["SC0"] = state["SC"]
                state["PB"] = min(surplus, 2400)
            elif surplus < -50 and soc > state.get("SI", 10):
                state["SC"] = max(state.get("SI", 10), soc - 0.1)
                state["SC0"] = state["SC"]
                state["PB"] = max(surplus, -2400)
            else:
                state["PB"] = 0

            state["SC"] = round(state["SC"], 1)
            state["SC0"] = state["SC"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="SunEnergyXT 500 PRO Simulator")
    parser.add_argument("--ip", default="0.0.0.0", help="Bind IP (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8500, help="HTTP port (default: 8500 — use 80 to match real device)")
    parser.add_argument("--sn", default="TBsimulator0001", help="Simulated serial number")
    parser.add_argument("--no-mdns", action="store_true", help="Disable mDNS announcement")
    parser.add_argument("--no-dynamics", action="store_true", help="Disable PV/SOC simulation loop")
    args = parser.parse_args()

    lan_ip = get_local_ip()

    with state_lock:
        state["SN"] = args.sn
        state["IP"] = lan_ip

    log.info("=" * 60)
    log.info("SunEnergyXT 500 PRO Simulator")
    log.info("=" * 60)
    log.info("SN       : %s", args.sn)
    log.info("LAN IP   : %s", lan_ip)
    log.info("Port     : %d", args.port)
    log.info("")
    log.info("Endpoints:")
    log.info("  GET  http://%s:%d/read", lan_ip, args.port)
    log.info("  POST http://%s:%d/write", lan_ip, args.port)
    log.info("")
    log.info("Simulator extras (not on real device):")
    log.info("  GET  http://%s:%d/sim/state   → full state", lan_ip, args.port)
    log.info("  GET  http://%s:%d/sim/writes  → write log", lan_ip, args.port)
    log.info("  POST http://%s:%d/sim/set     → inject state", lan_ip, args.port)
    log.info("=" * 60)

    zeroconf = None
    if not args.no_mdns:
        try:
            zeroconf = start_mdns(args.sn, lan_ip, args.port)
        except Exception as e:
            log.warning("mDNS failed (non-fatal): %s", e)
            log.warning("Add the device manually via IP in HA.")

    if not args.no_dynamics:
        t = threading.Thread(target=simulate_dynamics, daemon=True)
        t.start()
        log.info("🌤️  PV/SOC dynamics simulation running")

    try:
        app.run(host=args.ip, port=args.port, debug=False, use_reloader=False)
    finally:
        if zeroconf:
            zeroconf.unregister_all_services()
            zeroconf.close()


if __name__ == "__main__":
    main()
