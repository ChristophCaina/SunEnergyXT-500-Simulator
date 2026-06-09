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
    "BN": 1,                  # 1 battery pack total
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
# Simulator configuration — PV strings and battery topology
# ---------------------------------------------------------------------------
sim_pv_config = [
    {"active": False, "max_w": 625, "label": "PV1"},
    {"active": False, "max_w": 625, "label": "PV2"},
    {"active": False, "max_w": 625, "label": "PV3"},
    {"active": False, "max_w": 625, "label": "PV4"},
]
sim_battery_config = {
    "packs": 1,           # number of battery packs (BN)
    "capacity_wh": 5000,  # total usable capacity in Wh
    "max_charge_w": 800,  # max charge power per pack
}


def _apply_pv_state():
    """Recalculate PV state from sim_pv_config."""
    with state_lock:
        total_pv = 0
        for i, cfg in enumerate(sim_pv_config, 1):
            if cfg["active"]:
                # Simulate current MPPT output based on time of day (sinus curve)
                import math
                hour = datetime.now(UTC).hour + datetime.now(UTC).minute / 60
                # Peak at solar noon (12:00), zero before 6:00 and after 20:00
                angle = math.pi * (hour - 6) / 14
                factor = max(0.0, math.sin(angle))
                pwr = round(cfg["max_w"] * factor)
            else:
                pwr = 0
            state[f"PV{i}"] = pwr
            # Simulate voltage/current when active
            state[f"VP{i}"] = round(45.0 * (pwr / max(cfg["max_w"], 1)), 1) if pwr > 0 else 0.0
            state[f"II{i}"] = round(pwr / 45.0, 1) if pwr > 0 else 0.0
            total_pv += pwr
        state["PV"] = total_pv


def _apply_battery_state():
    """Apply battery topology config to state."""
    with state_lock:
        packs = sim_battery_config["packs"]
        state["BN"] = packs
        state["ON"] = packs
        state["IS"] = sim_battery_config["max_charge_w"]  # already = kopfCount * maxWPerHead

        # SC0 is master (head unit), SC1..SC5 are slave/extension packs.
        # Expose SCn and BSn for each extension pack that is configured;
        # remove keys for slots that no longer exist (topology change).
        master_soc = state.get("SC", state.get("SC0", 50))
        for i in range(1, 6):
            if i < packs:
                # Extension pack present — report same SOC as master
                state[f"SC{i}"] = master_soc
                state[f"BS{i}"] = "4.0.5"
            else:
                # Slot not populated — remove so integration sees absence, not stale value
                state.pop(f"SC{i}", None)
                state.pop(f"BS{i}", None)


def _sync_extension_soc():
    """Sync SC1..SCn to match master SOC (SC/SC0) for all configured extension packs."""
    packs = sim_battery_config["packs"]
    master_soc = state.get("SC", state.get("SC0", 50))
    for i in range(1, packs):
        state[f"SC{i}"] = master_soc


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

    # Validate IS (max inverter power) — cap at hardware maximum based on pack count
    if "IS" in applied:
        with state_lock:
            max_inverter_w = sim_battery_config.get("max_charge_w", 2400)  # head units only
            if state["IS"] > max_inverter_w:
                log.warning("⚠️  IS=%dW exceeds hardware max %dW — capping", state["IS"], max_inverter_w)
                state["IS"] = max_inverter_w
                applied["IS"]["new"] = max_inverter_w

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

    # Update MS (meter status) based on MM + MD state:
    # MS=1 (online) when MM=1 and MD is a non-empty string, else MS=0 (not bound).
    with state_lock:
        mm = state.get("MM", 0)
        md = state.get("MD", "")
        state["MS"] = 1 if (mm == 1 and md) else 0
        if "MM" in applied or "MD" in applied:
            log.info("🔌  METER STATUS  MS=%d  (MM=%d, MD=%s)", state["MS"], mm, repr(md[:40] if md else ""))

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


@app.route("/sim/ui")
def sim_ui():
    """Simulator Web UI — device configuration panel."""
    return SIM_UI_HTML


SIM_UI_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SunEnergyXT Simulator</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0e14;
    --surface: #111822;
    --border: #1e2d3d;
    --accent: #f0a500;
    --accent2: #00c2ff;
    --green: #00e676;
    --red: #ff3d57;
    --text: #cdd9e5;
    --muted: #4a5568;
    --mono: 'Share Tech Mono', monospace;
    --sans: 'Barlow', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; }

  .header {
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    align-items: center;
    gap: 16px;
    background: var(--surface);
  }
  .header-logo {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--accent);
    letter-spacing: 3px;
    text-transform: uppercase;
  }
  .header-title {
    font-size: 15px;
    font-weight: 600;
    color: var(--text);
  }
  .header-status {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .main { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 24px 32px; max-width: 1200px; }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
  }
  .card-header {
    padding: 12px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--accent);
  }
  .card-body { padding: 20px; }

  /* Live metrics */
  .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .metric { text-align: center; padding: 12px 8px; border: 1px solid var(--border); border-radius: 3px; }
  .metric-val { font-family: var(--mono); font-size: 22px; color: var(--accent2); }
  .metric-val.positive { color: var(--green); }
  .metric-val.negative { color: var(--red); }
  .metric-label { font-size: 10px; color: var(--muted); letter-spacing: 1px; text-transform: uppercase; margin-top: 4px; }

  /* PV Strings */
  .pv-grid { display: grid; gap: 14px; }
  .pv-row {
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 14px 16px;
    transition: border-color 0.2s;
  }
  .pv-row.active { border-color: var(--accent); }
  .pv-row-top { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .pv-label { font-family: var(--mono); font-size: 13px; color: var(--accent); min-width: 36px; }
  .pv-power { font-family: var(--mono); font-size: 12px; color: var(--accent2); margin-left: auto; min-width: 60px; text-align: right; }

  .toggle {
    position: relative; width: 40px; height: 22px; cursor: pointer;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-track {
    position: absolute; inset: 0; background: var(--border); border-radius: 11px; transition: 0.2s;
  }
  .toggle input:checked + .toggle-track { background: var(--accent); }
  .toggle-thumb {
    position: absolute; left: 3px; top: 3px; width: 16px; height: 16px;
    background: white; border-radius: 50%; transition: 0.2s;
  }
  .toggle input:checked ~ .toggle-thumb { left: 21px; }

  .slider-row { display: flex; align-items: center; gap: 10px; }
  .slider-row label { font-size: 11px; color: var(--muted); min-width: 80px; }
  input[type=range] {
    flex: 1; -webkit-appearance: none; height: 3px; background: var(--border); border-radius: 2px; outline: none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%; background: var(--accent); cursor: pointer;
  }
  .slider-val { font-family: var(--mono); font-size: 11px; color: var(--text); min-width: 50px; text-align: right; }

  /* Battery config */
  .bat-grid { display: grid; gap: 16px; }
  .bat-row { display: flex; align-items: center; gap: 12px; }
  .bat-row label { font-size: 12px; color: var(--muted); min-width: 140px; }
  .bat-row input[type=number] {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    font-family: var(--mono); font-size: 13px; padding: 6px 10px; border-radius: 3px; width: 100px;
  }
  .bat-row input[type=number]:focus { outline: none; border-color: var(--accent); }

  /* Scenarios */
  .scenario-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .btn {
    background: transparent; border: 1px solid var(--border); color: var(--text);
    font-family: var(--mono); font-size: 11px; letter-spacing: 1px;
    padding: 10px 14px; border-radius: 3px; cursor: pointer; transition: 0.15s;
    text-align: left;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn .btn-icon { font-size: 16px; display: block; margin-bottom: 4px; }
  .btn-apply {
    width: 100%; margin-top: 16px; padding: 11px;
    background: var(--accent); color: #000; border: none;
    font-family: var(--mono); font-size: 12px; letter-spacing: 2px;
    border-radius: 3px; cursor: pointer; font-weight: 700; transition: 0.15s;
  }
  .btn-apply:hover { background: #ffb700; }

  .toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--green); color: #000;
    font-family: var(--mono); font-size: 12px;
    padding: 10px 18px; border-radius: 3px;
    opacity: 0; transition: opacity 0.3s; pointer-events: none;
  }
  .toast.show { opacity: 1; }

  .full-width { grid-column: 1 / -1; }
  .soc-bar-wrap { margin-top: 8px; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
  .soc-bar { height: 100%; background: var(--green); border-radius: 3px; transition: width 2s; }
  .model-switch {
    display: flex; align-items: center; gap: 0;
    border: 1px solid var(--border); border-radius: 3px; overflow: hidden;
    margin-left: 24px;
  }
  .model-btn {
    padding: 6px 14px; font-family: var(--mono); font-size: 11px;
    letter-spacing: 1px; cursor: pointer; border: none;
    background: transparent; color: var(--muted); transition: 0.15s;
  }
  .model-btn.active { background: var(--accent); color: #000; font-weight: 700; }
  .model-btn:not(.active):hover { color: var(--text); }
  .kopf-btn {
    width: 48px; height: 48px; border-radius: 3px;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--muted); font-family: var(--mono); font-size: 18px;
    cursor: pointer; transition: 0.15s;
  }
  .kopf-btn.active { border-color: var(--accent); color: var(--accent); background: rgba(240,165,0,0.1); }
  .kopf-btn:not(.active):hover { border-color: var(--text); color: var(--text); }
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-logo">SunEnergyXT</div>
    <div class="header-title">Simulator Control Panel</div>
  </div>
  <div class="model-switch">
    <button class="model-btn" id="btn-std" onclick="setModel(1)">500</button>
    <button class="model-btn active" id="btn-pro" onclick="setModel(2)">500 PRO</button>
  </div>
  <div class="header-status">
    <div class="dot"></div>
    <span id="hdr-sn">—</span>
  </div>
</div>

<div class="main">

  <!-- Live Metrics -->
  <div class="card full-width">
    <div class="card-header">⚡ Live Status</div>
    <div class="card-body">
      <div class="metrics">
        <div class="metric">
          <div class="metric-val" id="m-pv">— W</div>
          <div class="metric-label">PV Gesamt</div>
        </div>
        <div class="metric">
          <div class="metric-val" id="m-pb">— W</div>
          <div class="metric-label">Batterie</div>
        </div>
        <div class="metric">
          <div class="metric-val" id="m-gp">— W</div>
          <div class="metric-label">Netz</div>
        </div>
        <div class="metric">
          <div class="metric-val" id="m-sc">— %</div>
          <div class="metric-label">SOC</div>
          <div class="soc-bar-wrap"><div class="soc-bar" id="soc-bar" style="width:0%"></div></div>
        </div>
        <div class="metric">
          <div class="metric-val" id="m-meter">— W</div>
          <div class="metric-label">Zähler (HA)</div>
        </div>
        <div class="metric">
          <div class="metric-val" id="m-iw">— W</div>
          <div class="metric-label">Eingang</div>
        </div>
        <div class="metric">
          <div class="metric-val" id="m-mm">—</div>
          <div class="metric-label">Modus</div>
        </div>
      </div>
    </div>
  </div>

  <!-- PV Strings -->
  <div class="card">
    <div class="card-header">☀️ PV Strings</div>
    <div class="card-body">
      <div class="pv-grid" id="pv-grid"></div>
      <button class="btn-apply" onclick="applyPV()">ÜBERNEHMEN</button>
    </div>
  </div>

  <!-- Battery Topology -->
  <div class="card">
    <div class="card-header">🔋 Batterie-Topologie</div>
    <div class="card-body">
      <div class="bat-grid">
        <div class="bat-row" style="flex-direction:column;align-items:flex-start;gap:8px;">
          <label style="min-width:unset;">Kopfspeicher <span style="color:var(--muted);font-size:10px;">MAX. 3 · je 2,4 kW · 5 kWh</span></label>
          <div style="display:flex;gap:8px;">
            <button class="kopf-btn active" id="kopf-1" onclick="setKopf(1)">1</button>
            <button class="kopf-btn" id="kopf-2" onclick="setKopf(2)">2</button>
            <button class="kopf-btn" id="kopf-3" onclick="setKopf(3)">3</button>
          </div>
        </div>
        <div class="bat-row" style="flex-direction:column;align-items:flex-start;gap:8px;">
          <label style="min-width:unset;">Erweiterungsspeicher <span style="color:var(--muted);font-size:10px;">MAX. 15 · je 5 kWh</span></label>
          <div style="display:flex;align-items:center;gap:10px;width:100%;">
            <input type="range" id="erw-slider" min="0" max="15" step="1" value="0" oninput="setErw(this.value)" style="flex:1;">
            <span class="slider-val" id="erw-val">0 Stk.</span>
          </div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:3px;padding:12px;font-family:var(--mono);font-size:12px;">
          <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
            <span style="color:var(--muted);">Gesamtkapazität</span>
            <span style="color:var(--accent2);" id="bat-total-cap">5 kWh</span>
          </div>
          <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
            <span style="color:var(--muted);">Gesamtleistung</span>
            <span style="color:var(--accent2);" id="bat-total-pwr">2,4 kW</span>
          </div>
          <div style="display:flex;justify-content:space-between;">
            <span style="color:var(--muted);">Batteriepacks (BN)</span>
            <span style="color:var(--accent);" id="bat-total-packs">1</span>
          </div>
        </div>
        <div class="bat-row">
          <label>SOC manuell setzen</label>
          <input type="number" id="bat-soc" min="0" max="100" step="1" value="15">
          <span style="font-size:11px;color:var(--muted)">%</span>
        </div>
      </div>
      <button class="btn-apply" onclick="applyBattery()">ÜBERNEHMEN</button>
    </div>
  </div>

  <!-- Scenarios -->
  <div class="card full-width">
    <div class="card-header">🎬 Szenarien</div>
    <div class="card-body">
      <div class="scenario-grid">
        <button class="btn" onclick="scenario('morning')"><span class="btn-icon">🌅</span>Morgen (schwache Sonne)</button>
        <button class="btn" onclick="scenario('noon')"><span class="btn-icon">☀️</span>Solarer Mittag (max. PV)</button>
        <button class="btn" onclick="scenario('cloudy')"><span class="btn-icon">⛅</span>Bewölkt (30% PV)</button>
        <button class="btn" onclick="scenario('night')"><span class="btn-icon">🌙</span>Nacht (kein PV)</button>
        <button class="btn" onclick="scenario('full')"><span class="btn-icon">✅</span>Batterie voll (SOC 95%)</button>
        <button class="btn" onclick="scenario('empty')"><span class="btn-icon">🪫</span>Batterie leer (SOC 10%)</button>
      </div>
    </div>
  </div>

</div>

<div class="toast" id="toast">✓ Übernommen</div>

<script>
// --- PV Config State ---
let pvConfig = [
  {active: false, max_w: 625, label: "PV1"},
  {active: false, max_w: 625, label: "PV2"},
  {active: false, max_w: 625, label: "PV3"},
  {active: false, max_w: 625, label: "PV4"},
];

function buildPVGrid() {
  const grid = document.getElementById('pv-grid');
  grid.innerHTML = '';
  pvConfig.forEach((pv, i) => {
    grid.innerHTML += `
    <div class="pv-row ${pv.active ? 'active' : ''}" id="pvrow-${i}">
      <div class="pv-row-top">
        <span class="pv-label">${pv.label}</span>
        <label class="toggle">
          <input type="checkbox" ${pv.active ? 'checked' : ''} onchange="togglePV(${i}, this.checked)">
          <div class="toggle-track"></div>
          <div class="toggle-thumb"></div>
        </label>
        <span class="pv-power" id="pvpwr-${i}">0 W</span>
      </div>
      <div class="slider-row">
        <label>Max. Leistung</label>
        <input type="range" min="100" max="2500" step="25" value="${pv.max_w}"
          oninput="setPVMax(${i}, this.value)" ${pv.active ? '' : 'disabled'}>
        <span class="slider-val" id="pvmax-${i}">${pv.max_w} W</span>
      </div>
    </div>`;
  });
}

function togglePV(i, val) {
  pvConfig[i].active = val;
  document.getElementById(`pvrow-${i}`).classList.toggle('active', val);
  document.querySelector(`#pvrow-${i} input[type=range]`).disabled = !val;
}

function setPVMax(i, val) {
  pvConfig[i].max_w = parseInt(val);
  document.getElementById(`pvmax-${i}`).textContent = val + ' W';
}

// --- Battery Topology ---
let kopfCount = 1;
let erwCount = 0;

function setKopf(n) {
  kopfCount = n;
  [1,2,3].forEach(i => {
    document.getElementById(`kopf-${i}`).classList.toggle('active', i === n);
  });
  updateBatSummary();
}

function setErw(n) {
  erwCount = parseInt(n);
  document.getElementById('erw-val').textContent = erwCount + ' Stk.';
  updateBatSummary();
}

function updateBatSummary() {
  const totalPacks = kopfCount + erwCount;
  const totalCap = totalPacks * 5;
  const totalPwr = kopfCount * 2.4;
  document.getElementById('bat-total-cap').textContent = totalCap + ' kWh';
  document.getElementById('bat-total-pwr').textContent = totalPwr.toFixed(1).replace('.', ',') + ' kW';
  document.getElementById('bat-total-packs').textContent = totalPacks;
}

async function applyAll() {
  const totalPacks = kopfCount + erwCount;
  const totalCapWh = totalPacks * 5000;
  const currentModel = parseInt(document.getElementById('btn-pro')?.classList.contains('active') ? 2 : 1);
  const maxWPerHead = currentModel === 1 ? 800 : 2400;  // 500=800W, 500 PRO=2400W
  const maxChargeW = kopfCount * maxWPerHead;
  const soc = parseFloat(document.getElementById('bat-soc').value);

  await fetch('/sim/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      pv: pvConfig,
      battery: {packs: totalPacks, capacity_wh: totalCapWh, max_charge_w: maxChargeW}
    })
  });
  await fetch('/sim/set', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({SC: soc, SC0: soc, IS: maxChargeW, BN: totalPacks, ON: totalPacks})
  });
  showToast();
}

async function applyPV() { await applyAll(); }
async function applyBattery() { await applyAll(); }

// --- Scenarios ---
async function scenario(name) {
  const scenarios = {
    morning: {pv: [{active:true,max_w:625},{active:true,max_w:625},{active:false,max_w:625},{active:false,max_w:625}], pvFactor: 0.25},
    noon:    {pv: [{active:true,max_w:625},{active:true,max_w:625},{active:true,max_w:625},{active:true,max_w:625}], pvFactor: 1.0},
    cloudy:  {pv: [{active:true,max_w:625},{active:true,max_w:625},{active:false,max_w:625},{active:false,max_w:625}], pvFactor: 0.3},
    night:   {pv: [{active:false,max_w:625},{active:false,max_w:625},{active:false,max_w:625},{active:false,max_w:625}], pvFactor: 0},
    full:    null,
    empty:   null,
  };
  const s = scenarios[name];
  if (s && s.pv) {
    pvConfig = s.pv.map((p,i) => ({...p, label: `PV${i+1}`}));
    buildPVGrid();
    // Override PV values directly for instant effect
    let totalPV = 0;
    const pvSet = {};
    pvConfig.forEach((p, i) => {
      const pwr = p.active ? Math.round(p.max_w * s.pvFactor) : 0;
      pvSet[`PV${i+1}`] = pwr;
      totalPV += pwr;
    });
    pvSet.PV = totalPV;
    await fetch('/sim/set', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(pvSet)});
    await applyPV();
  }
  if (name === 'full') {
    await fetch('/sim/set', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({SC:95,SC0:95})});
  }
  if (name === 'empty') {
    await fetch('/sim/set', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({SC:10,SC0:10})});
  }
  showToast();
}

// --- Live metrics ---
async function updateMetrics() {
  try {
    const r = await fetch('/read');
    const d = await r.json();
    const s = d.state.reported;
    document.getElementById('hdr-sn').textContent = s.SN;
    setMetric('m-pv', s.PV, 'W');
    setMetric('m-pb', s.PB, 'W');
    setMetric('m-gp', s.GP, 'W');
    setMetric('m-sc', s.SC, '%');
    setMetric('m-iw', s.IW, 'W');
    document.getElementById('m-mm').textContent = s.MM === 1 ? 'AUTO' : s.GS !== 0 ? 'GS' : 'STANDBY';

    // Meter value from sim/state
    const sr = await fetch('/sim/state');
    const ss = await sr.json();
    if (ss._meter_power !== undefined) {
      setMetric('m-meter', ss._meter_power, 'W');
    }
    document.getElementById('soc-bar').style.width = Math.min(100, s.SC) + '%';

    // Update PV power display
    pvConfig.forEach((p, i) => {
      const el = document.getElementById(`pvpwr-${i}`);
      if (el) el.textContent = (s[`PV${i+1}`] || 0) + ' W';
    });
  } catch(e) {}
}

function setMetric(id, val, unit) {
  const el = document.getElementById(id);
  el.textContent = val + ' ' + unit;
  el.className = 'metric-val' + (val > 0 ? ' positive' : val < 0 ? ' negative' : '');
}

function showToast() {
  const t = document.getElementById('toast');
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

// Init
buildPVGrid();
updateMetrics();
setInterval(updateMetrics, 3000);

// Load existing config
fetch('/sim/config').then(r => r.json()).then(d => {
  if (d.pv) {
    pvConfig = d.pv;
    buildPVGrid();
  }
  if (d.battery) {
    // Restore kopf/erw from saved battery config
    // We store kopfCount separately; derive from max_charge_w
    const savedMaxW = d.battery.max_charge_w || 2400;
    const pk = d.state?.reported?.PK || 2;
    const maxWPerHead = pk === 1 ? 800 : 2400;  // 500=800W, 500 PRO=2400W
    kopfCount = Math.round(savedMaxW / maxWPerHead) || 1;
    const totalPacks = d.battery.packs || 1;
    erwCount = Math.max(0, totalPacks - kopfCount);
    setKopf(kopfCount);
    document.getElementById('erw-slider').value = erwCount;
    setErw(erwCount);
  }
  updateBatSummary();
});

// --- Model Switch ---
const MODEL_CONFIGS = {
  1: { PK: 1, maxPVW: 800, pvPairs: [[0,1],[2,3]], maxChargeW: 800,  label: '500' },
  2: { PK: 2, maxPVW: 625, pvPairs: null,           maxChargeW: 2400, label: '500 PRO' },
};
let currentModel = 2;

async function setModel(pk) {
  currentModel = pk;
  const cfg = MODEL_CONFIGS[pk];

  // Update UI buttons
  document.getElementById('btn-std').classList.toggle('active', pk === 1);
  document.getElementById('btn-pro').classList.toggle('active', pk === 2);

  // Update PV max per string
  pvConfig.forEach((p, i) => {
    p.max_w = cfg.maxPVW;
  });
  buildPVGrid();

  // Update battery max charge in UI
  document.getElementById('bat-maxw').value = cfg.maxChargeW;

  // Apply both PV and battery config + PK/IS in one go
  const totalPacks = kopfCount + erwCount;
  const totalCapWh = totalPacks * 5000;

  await fetch('/sim/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      pv: pvConfig,
      battery: {packs: totalPacks, capacity_wh: totalCapWh, max_charge_w: cfg.maxChargeW}
    })
  });

  await fetch('/sim/set', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({PK: cfg.PK, IS: cfg.maxChargeW})
  });

  showToast();
}

// Init model from current state
fetch('/read').then(r => r.json()).then(d => {
  const pk = d.state.reported.PK || 2;
  currentModel = pk;
  document.getElementById('btn-std').classList.toggle('active', pk === 1);
  document.getElementById('btn-pro').classList.toggle('active', pk === 2);
});
</script>
</body>
</html>"""


@app.route("/sim/config", methods=["GET", "POST"])
def sim_config():
    """Simulator config: PV strings and battery topology."""
    global sim_pv_config, sim_battery_config
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "pv" in body:
            sim_pv_config = body["pv"]
        if "battery" in body:
            sim_battery_config = body["battery"]
        _apply_pv_state()
        _apply_battery_state()
        return jsonify({"ok": True})
    return jsonify({"pv": sim_pv_config, "battery": sim_battery_config})


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
            # Enforce hardware limits on power fields
            if k == "PB":
                max_w = sim_battery_config.get("max_charge_w", 2400)  # head units only
                v = max(-max_w, min(max_w, v))
            elif k == "IS":
                max_w = sim_battery_config.get("max_charge_w", 2400)  # head units only
                v = min(max_w, v)
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
    soc_step = 0.05           # SOC % per 5s tick at 800W into ~5kWh battery
    TICK_S = 5
    METER_POLL_S = 1          # poll meter every 1s (like real device)

    last_meter_poll = 0.0
    last_total_power: float | None = None

    while True:
        time.sleep(TICK_S)
        now = time.monotonic()

        # --- Update PV from active string config ---
        _apply_pv_state()

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
                with state_lock:
                    state["_meter_power"] = round(last_total_power)
                log.debug("📡 Meter poll → total_power=%.1fW", last_total_power)

        with state_lock:
            soc = state["SC"]
            sa = state.get("SA", 95)
            si = state.get("SI", 10)
            gs = state.get("GS", 0)
            pv = state.get("PV", 0)
            MAX_POWER_W = state.get("IS", 2400)
            capacity_wh = sim_battery_config.get("capacity_wh", 5000)
            soc_step = (800 * TICK_S / 3600) / capacity_wh * 100

            if meter_url and last_total_power is not None:
                # ---- Self-consumption mode (MM=1): regulate to zero grid flow ----
                # total_power > 0 → surplus being exported → charge battery
                # total_power < 0 → grid import → discharge battery
                target_power = last_total_power
                # PV production adds to available charge power
                battery_power = max(-MAX_POWER_W, min(MAX_POWER_W, target_power + pv))

                if battery_power > 0:
                    # Charge battery with surplus PV
                    if soc < sa:
                        new_soc = min(sa, soc + (battery_power / 800) * soc_step)
                        state["SC"] = round(new_soc, 2)
                        state["SC0"] = state["SC"]
                        _sync_extension_soc()
                        state["PB"] = round(battery_power)
                        state["GP"] = round(target_power - (battery_power - pv))
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
                        new_soc = max(si, soc - (discharge / 800) * soc_step)
                        state["SC"] = round(new_soc, 2)
                        state["SC0"] = state["SC"]
                        _sync_extension_soc()
                        state["PB"] = -round(discharge)
                        state["GP"] = round(target_power + discharge + pv)
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
                    new_soc = min(sa, soc + (charge_power / 800) * soc_step)
                    state["SC"] = round(new_soc, 2)
                    state["SC0"] = state["SC"]
                    _sync_extension_soc()
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
                    new_soc = max(si, soc - (discharge_power / 800) * soc_step)
                    state["SC"] = round(new_soc, 2)
                    state["SC0"] = state["SC"]
                    _sync_extension_soc()
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
                    new_soc = min(sa, soc + soc_step)
                    state["SC"] = round(new_soc, 2)
                    state["SC0"] = state["SC"]
                    _sync_extension_soc()
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
# Midnight reset — clear daily energy counters like the real device
# ---------------------------------------------------------------------------
def midnight_reset_loop():
    """Reset daily energy counters at midnight, like the real device."""
    last_date = datetime.now(UTC).date()
    while True:
        time.sleep(30)
        today = datetime.now(UTC).date()
        if today != last_date:
            with state_lock:
                state["GD1"] = 0
                state["GD2"] = 0
                state["PD"] = 0
                state["LD"] = 0
            log.info("🌅 Midnight reset — daily energy counters cleared")
            last_date = today


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
    hostname = f"SunEnergyXT_AIO_{sn}.local."
    info = ServiceInfo(
        type_="_http._tcp.local.",
        name=f"SunEnergyXT_AIO_{sn}._http._tcp.local.",
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
# State persistence — save/load to JSON so restarts don't reset SOC etc.
# ---------------------------------------------------------------------------
PERSIST_FILE = "/opt/sunenergyxt-simulator/sim_state.json"
PERSIST_KEYS = {"SC", "SC0", "GD1", "GD2", "GS", "IS", "SI", "SA", "SO",
                "LM", "MM", "MD", "MS", "PD", "LD"}
PERSIST_INTERVAL_S = 30


def load_persisted_state():
    """Load persisted state from disk, merge into current state."""
    global sim_pv_config, sim_battery_config
    try:
        with open(PERSIST_FILE) as f:
            data = json.load(f)
        if "pv" in data:
            sim_pv_config = data["pv"]
        if "battery" in data:
            sim_battery_config = data["battery"]
        with state_lock:
            for k, v in data.get("state", {}).items():
                if k in PERSIST_KEYS:
                    state[k] = v
            # Enforce hardware limits — battery config must be loaded first
            max_inverter_w = sim_battery_config.get("max_charge_w", 2400)  # head units only
            if state.get("IS", 2400) > max_inverter_w:
                log.warning("💾 Persisted IS=%dW exceeds hardware max %dW — capping",
                            state["IS"], max_inverter_w)
                state["IS"] = max_inverter_w
        log.info("💾 State restored from %s (SOC=%.1f%%)", PERSIST_FILE, state.get("SC", 0))
    except FileNotFoundError:
        log.info("💾 No persisted state found — starting fresh")
    except Exception as e:
        log.warning("💾 Failed to load state: %s", e)


def save_persisted_state():
    """Save relevant state fields to disk."""
    try:
        with state_lock:
            to_save = {k: state[k] for k in PERSIST_KEYS if k in state}
        data = {"state": to_save, "pv": sim_pv_config, "battery": sim_battery_config}
        with open(PERSIST_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("💾 Failed to save state: %s", e)


def persist_loop():
    """Background thread: save state every 30s."""
    while True:
        time.sleep(PERSIST_INTERVAL_S)
        save_persisted_state()



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
        state["COM"] = args.port

    load_persisted_state()
    _apply_battery_state()  # ensure BN/ON/SCn consistent with sim_battery_config at startup

    # Restore MS based on persisted MM + MD
    with state_lock:
        mm = state.get("MM", 0)
        md = state.get("MD", "")
        state["MS"] = 1 if (mm == 1 and md) else 0

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

    m = threading.Thread(target=midnight_reset_loop, daemon=True)
    m.start()
    log.info("🌅 Midnight reset active — daily counters reset at 00:00 UTC")

    p = threading.Thread(target=persist_loop, daemon=True)
    p.start()
    log.info("💾 State persistence active — saving every %ds to %s", PERSIST_INTERVAL_S, PERSIST_FILE)

    try:
        app.run(host=args.ip, port=args.port, debug=False, use_reloader=False)
    finally:
        save_persisted_state()
        log.info("💾 State saved on shutdown")
        if zeroconf:
            zeroconf.unregister_all_services()
            zeroconf.close()


if __name__ == "__main__":
    main()
