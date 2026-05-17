#!/usr/bin/env python3
"""
SunEnergyXT 500 PRO — Device Simulator

Simulates the local HTTP API of the SunEnergyXT 500 Series device,
including mDNS/Zeroconf announcement so Home Assistant can discover it
automatically — just like the real device.

Realistic starting state:
- No PV modules connected
- No meter configured (MS=0, MD empty)
- Battery empty (SOC=15%), charging from grid at ~800W
- GS=0 (no setpoint from HA yet)

Usage:
    python simulator.py [--ip 0.0.0.0] [--port 80] [--sn TBsimulator0001]

Requirements:
    pip install flask zeroconf

Endpoints implemented:
    GET  /read       → returns current device state
    POST /write      → accepts partial state updates (GS, IS, SI, SA, etc.)

Simulator-only endpoints (not on real device):
    GET  /sim/state  → full current state
    GET  /sim/writes → write history
    POST /sim/set    → inject arbitrary state changes
"""

import argparse
import json
import logging
import socket
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, UTC

from flask import Flask, jsonify, request
from zeroconf import ServiceInfo, Zeroconf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("simulator")

# ---------------------------------------------------------------------------
# Default device state — freshly installed 500 PRO, no PV, no meter
# ---------------------------------------------------------------------------
DEFAULT_STATE: dict = {
    # Identity
    "SN": "TBsimulator0001",
    "PK": 2,                  # 2 = 500 Pro (2400W)
    "DevType": "SunEnergyXT500Pro",

    # System status
    "ST": 2,                  # 2 = Running
    "WT": 3,                  # Wi-Fi connected

    # PV input — no modules connected
    "PV": 0,
    "PV1": 0, "PV2": 0, "PV3": 0, "PV4": 0,
    "II1": 0.0, "II2": 0.0, "II3": 0.0, "II4": 0.0,
    "VP1": 0.0, "VP2": 0.0, "VP3": 0.0, "VP4": 0.0,

    # Power flow — charging from grid at max charge power
    "IW": 800,                # total input = grid charging
    "OP": 0,                  # no output (not discharging)
    "GP": -800,               # negative = drawing from grid
    "LP": 0,                  # no load port
    "PB": 800,                # battery charging at 800W

    # Battery — freshly installed, low SOC
    "SC": 15,                 # total SOC %
    "SC0": 15,                # head unit SOC
    "ON": 1,                  # 1 battery pack online

    # Setpoints (writable)
    "GS": 0,                  # no setpoint from HA yet
    "IS": 2400,               # max inverter power
    "SI": 10,                 # min discharge SOC
    "SA": 95,                 # max charge SOC
    "SO": 10,                 # min off-grid discharge SOC
    "PT": 1440,               # auto-shutdown time

    # Modes
    "LM": 1,                  # local mode on
    "MM": 0,                  # self-consumption mode off (no meter)
    "PM": 0,                  # no parallel mode

    # Meter — not configured
    "MS": 0,                  # 0 = not bound
    "MD": "",                 # no meter connection string

    # Energy counters (Wh, raw)
    "PD": 0,                  # no PV today
    "GD1": 0,                 # grid charge today (updated by dynamics)
    "GD2": 0,                 # grid feed-in today
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
app.logger.setLevel(logging.WARNING)


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
    """Simulator only: inspect full state."""
    with state_lock:
        return jsonify(state)


@app.route("/sim/writes", methods=["GET"])
def sim_writes():
    """Simulator only: inspect all received /write calls."""
    return jsonify(write_log)


@app.route("/sim/set", methods=["POST"])
def sim_set():
    """
    Simulator only: inject arbitrary state changes.

    Example — simulate 2000W solar arriving:
        curl -X POST http://device-ip/sim/set \\
             -H 'Content-Type: application/json' \\
             -d '{"PV": 2000, "GP": 1200}'
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
# Meter polling — reads total_power from HA proxy URL (like the real device)
# ---------------------------------------------------------------------------
def _poll_meter_url(url: str) -> float | None:
    """
    Poll the HA proxy endpoint and return total_power in Watts.
    Returns None on any error.
    Sign convention: positive = export to grid, negative = import from grid.
    """
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            return float(data["total_power"])
    except (urllib.error.URLError, KeyError, ValueError, OSError) as e:
        log.warning("⚠️  Meter poll failed (%s): %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Dynamics simulation loop
# ---------------------------------------------------------------------------
def simulate_dynamics():
    """
    Simulates a SunEnergyXT 500 PRO with two operating modes:

    MM=1 + MD set (self-consumption mode):
        Polls the HA proxy URL configured in MD, reads total_power and
        uses it as the regulation target — exactly like the real device.
        Positive total_power = grid export → battery charges to absorb surplus.
        Negative total_power = grid import → battery discharges to cover demand.

    MM=0 or MD empty (manual / GS mode):
        Falls back to GS setpoint behaviour:
        GS < 0 → charge from grid, GS > 0 → discharge to grid, GS = 0 → auto.
    """
    CHARGE_POWER_W = 800      # default grid charge power when GS=0
    MAX_POWER_W = 2400        # max inverter power
    SOC_STEP = 0.05           # SOC % per 5s tick at 800W into ~5kWh battery
    TICK_S = 5
    METER_POLL_S = 1          # poll meter every 1s (like real device)

    last_meter_poll = 0.0
    last_total_power: float | None = None

    while True:
        time.sleep(TICK_S)
        now = time.monotonic()

        # --- Meter polling (MM=1 mode) ---
        with state_lock:
            mm = state.get("MM", 0)
            md = state.get("MD", "")

        meter_url = None
        if mm == 1 and md:
            # Extract dat_url from MD JSON string (set by HA integration)
            try:
                md_cfg = json.loads(md)
                meter_url = md_cfg.get("direct", {}).get("dat_url")
            except (json.JSONDecodeError, AttributeError):
                # MD might be a plain URL string in some configs
                if md.startswith("http"):
                    meter_url = md

        if meter_url and (now - last_meter_poll >= METER_POLL_S):
            last_total_power = _poll_meter_url(meter_url)
            last_meter_poll = now
            if last_total_power is not None:
                log.debug("📡 Meter poll → total_power=%.1fW", last_total_power)

        with state_lock:
            soc = state["SC"]
            sa = state.get("SA", 95)
            si = state.get("SI", 10)
            gs = state.get("GS", 0)
            pv = state.get("PV", 0)

            if meter_url and last_total_power is not None:
                # ---- Self-consumption mode (MM=1): regulate to zero grid flow ----
                # total_power > 0 → surplus being exported → charge battery
                # total_power < 0 → grid import → discharge battery
                target_power = last_total_power
                battery_power = max(-MAX_POWER_W, min(MAX_POWER_W, target_power))

                if battery_power > 0:
                    # Charge battery with surplus PV
                    if soc < sa:
                        new_soc = min(sa, soc + (battery_power / 800) * SOC_STEP)
                        state["SC"] = round(new_soc, 2)
                        state["SC0"] = state["SC"]
                        state["PB"] = round(battery_power)
                        state["GP"] = round(target_power - battery_power)
                        state["IW"] = pv + round(battery_power)
                        state["OP"] = 0
                        state["GD1"] = round(
                            state.get("GD1", 0) + (battery_power * TICK_S / 3600), 1
                        )
                        log.debug("🔋 [MM] Charging %.0fW (surplus=%.0fW), SOC=%.1f%%",
                                  battery_power, target_power, state["SC"])
                    else:
                        # Battery full — surplus goes to grid
                        state["PB"] = 0
                        state["GP"] = round(target_power)
                        state["IW"] = pv
                        state["OP"] = 0
                        log.debug("✅ [MM] Battery full, surplus %.0fW to grid", target_power)

                elif battery_power < 0:
                    # Discharge battery to cover grid import
                    discharge = abs(battery_power)
                    if soc > si:
                        new_soc = max(si, soc - (discharge / 800) * SOC_STEP)
                        state["SC"] = round(new_soc, 2)
                        state["SC0"] = state["SC"]
                        state["PB"] = -round(discharge)
                        state["GP"] = round(target_power + discharge)
                        state["IW"] = pv
                        state["OP"] = round(discharge)
                        state["GD2"] = round(
                            state.get("GD2", 0) + (discharge * TICK_S / 3600), 1
                        )
                        log.debug("⚡ [MM] Discharging %.0fW (import=%.0fW), SOC=%.1f%%",
                                  discharge, abs(target_power), state["SC"])
                    else:
                        # Battery empty — grid covers the rest
                        state["PB"] = 0
                        state["GP"] = round(target_power)
                        state["IW"] = pv
                        state["OP"] = 0
                        log.debug("🪫 [MM] Battery empty, grid covers %.0fW", abs(target_power))
                else:
                    # Balanced — no action needed
                    state["PB"] = 0
                    state["GP"] = 0
                    state["IW"] = pv
                    state["OP"] = 0

            elif gs < 0:
                # ---- Manual mode: HA commanded grid import (charge battery) ----
                charge_power = min(abs(gs), MAX_POWER_W)
                if soc < sa:
                    new_soc = min(sa, soc + (charge_power / 800) * SOC_STEP)
                    state["SC"] = round(new_soc, 2)
                    state["SC0"] = state["SC"]
                    state["PB"] = charge_power
                    state["GP"] = -charge_power
                    state["IW"] = charge_power + pv
                    state["OP"] = 0
                    state["GD1"] = round(
                        state.get("GD1", 0) + (charge_power * TICK_S / 3600), 1
                    )
                    log.debug("🔋 Charging at %dW (GS=%d), SOC=%.1f%%",
                              charge_power, gs, state["SC"])
                else:
                    state["PB"] = 0
                    state["GP"] = 0
                    state["IW"] = pv
                    state["OP"] = 0

            elif gs > 0:
                # ---- Manual mode: HA commanded grid export (discharge battery) ----
                discharge_power = min(gs, MAX_POWER_W)
                if soc > si:
                    new_soc = max(si, soc - (discharge_power / 800) * SOC_STEP)
                    state["SC"] = round(new_soc, 2)
                    state["SC0"] = state["SC"]
                    state["PB"] = -discharge_power
                    state["GP"] = discharge_power + pv
                    state["IW"] = pv
                    state["OP"] = discharge_power
                    state["GD2"] = round(
                        state.get("GD2", 0) + (discharge_power * TICK_S / 3600), 1
                    )
                    log.debug("⚡ Discharging at %dW (GS=%d), SOC=%.1f%%",
                              discharge_power, gs, state["SC"])
                else:
                    state["PB"] = 0
                    state["GP"] = pv
                    state["IW"] = pv
                    state["OP"] = pv

            else:
                # ---- GS=0: device auto — charge from grid if SOC < SA ----
                if soc < sa:
                    new_soc = min(sa, soc + SOC_STEP)
                    state["SC"] = round(new_soc, 2)
                    state["SC0"] = state["SC"]
                    state["PB"] = CHARGE_POWER_W
                    state["GP"] = -(CHARGE_POWER_W - pv)
                    state["IW"] = CHARGE_POWER_W
                    state["OP"] = 0
                    state["GD1"] = round(
                        state.get("GD1", 0) + (CHARGE_POWER_W * TICK_S / 3600), 1
                    )
                    log.debug("🔋 Auto-charging at %dW (GS=0), SOC=%.1f%%",
                              CHARGE_POWER_W, state["SC"])
                else:
                    state["PB"] = 0
                    state["GP"] = pv
                    state["IW"] = pv
                    state["OP"] = pv
                    log.debug("✅ Battery full (SOC=%.1f%%), standby", soc)


# ---------------------------------------------------------------------------
# mDNS / Zeroconf announcement
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_mdns(sn: str, ip: str, port: int) -> Zeroconf:
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
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="SunEnergyXT 500 PRO Simulator")
    parser.add_argument("--ip", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--sn", default="TBsimulator0001")
    parser.add_argument("--no-mdns", action="store_true")
    parser.add_argument("--no-dynamics", action="store_true")
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
    log.info("Start    : SOC=15%%, no PV, charging from grid")
    log.info("")
    log.info("Endpoints:")
    log.info("  GET  http://%s:%d/read", lan_ip, args.port)
    log.info("  POST http://%s:%d/write", lan_ip, args.port)
    log.info("")
    log.info("Simulator extras:")
    log.info("  GET  http://%s:%d/sim/state", lan_ip, args.port)
    log.info("  GET  http://%s:%d/sim/writes", lan_ip, args.port)
    log.info("  POST http://%s:%d/sim/set", lan_ip, args.port)
    log.info("=" * 60)
    log.info("Self-consumption mode (MM=1):")
    log.info("  HA integration sets MD → simulator polls HA proxy URL")
    log.info("  Regulates like real device — no manual GS writes needed")
    log.info("=" * 60)

    zeroconf = None
    if not args.no_mdns:
        try:
            zeroconf = start_mdns(args.sn, lan_ip, args.port)
        except Exception as e:
            log.warning("mDNS failed (non-fatal): %s", e)

    if not args.no_dynamics:
        t = threading.Thread(target=simulate_dynamics, daemon=True)
        t.start()
        log.info("🔋 Dynamics running — charging from grid, SOC starts at 15%%")

    try:
        app.run(host=args.ip, port=args.port, debug=False, use_reloader=False)
    finally:
        if zeroconf:
            zeroconf.unregister_all_services()
            zeroconf.close()


if __name__ == "__main__":
    main()