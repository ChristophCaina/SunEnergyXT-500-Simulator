# SunEnergyXT 500 PRO — Device Simulator

A lightweight simulator for testing the SunEnergyXT 500 Series Home Assistant
integration without a physical device.

## What it does

- Runs a local HTTP server that implements the same `/read` and `/write` API
  as the real device
- Announces itself via **mDNS/Zeroconf** so Home Assistant can discover it
  automatically — just like the real device
- Simulates a live PV/SOC dynamic (solar sine wave over a 24h cycle)
- Logs all `/write` calls to the console so you can verify GS is written correctly
- Provides extra `/sim/` endpoints to inject state and inspect write history

## Requirements

```bash
pip install flask zeroconf
```

## Usage

```bash
# Basic — listens on port 8500, announces via mDNS
python simulator.py

# Custom port (use 80 to match the real device exactly)
python simulator.py --port 80

# Custom serial number
python simulator.py --sn MyTestDevice001

# Without mDNS (manual IP entry in HA)
python simulator.py --no-mdns

# Without PV/SOC dynamics (static state)
python simulator.py --no-dynamics
```

## Configuring the HA integration against the simulator

When adding the integration in Home Assistant, enter the **LAN IP** of the
machine running the simulator. If using a non-standard port, the integration
currently uses port 80 — so either run the simulator on port 80 (may need
`sudo`) or use a port-forwarding tool like `socat`.

The simplest approach for testing: run the simulator on the same machine as
Home Assistant and use `127.0.0.1` as the IP.

## Simulator-only endpoints

These endpoints are **not** present on the real device. They exist only to
make testing easier.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sim/state` | GET | Full current state |
| `/sim/writes` | GET | All received `/write` calls with timestamps |
| `/sim/set` | POST | Inject arbitrary state changes |

### Example: simulate a grid import of 500W

```bash
curl -X POST http://localhost:8500/sim/set \
     -H 'Content-Type: application/json' \
     -d '{"GP": -500}'
```

### Example: check what GS value the integration wrote

```bash
curl http://localhost:8500/sim/writes
```

### Example: simulate low SOC

```bash
curl -X POST http://localhost:8500/sim/set \
     -H 'Content-Type: application/json' \
     -d '{"SC": 12, "SC0": 12}'
```

## Testing the grid sensor feature

1. Start the simulator
2. Add the integration in HA using the simulator's IP
3. In the optional grid sensor step, select your SolarEdge meter sensor
4. Watch the simulator console — every time the sensor changes, you should see:

```
✏️   WRITE  GS    0 → -350
✏️   WRITE  GS    -350 → -280
```

This confirms the integration is writing GS automatically on sensor state changes.
