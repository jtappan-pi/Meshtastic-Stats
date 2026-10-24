# RAK4631 Meshtastic Health Dashboard
### WisMesh Pocket v2 · 915 MHz · Windows & Linux

A BLE-connected dashboard that polls your RAK4631 for telemetry on demand
and visualises radio health stats in a local web interface.

---

## Prerequisites

- **Python 3.10+**
- **A PC with Bluetooth** (built-in or USB dongle) — Windows 11 or Linux
- **RAK4631 paired to the PC** (one-time setup — see below)

---

## One-Time: Pair the Radio

Only one BLE client can hold the radio at a time, so disconnect the Meshtastic
phone app (or turn the phone's Bluetooth off) before pairing and polling.

**Windows:** Settings → Bluetooth & devices → Add device → Bluetooth. The radio
appears as `Meshtastic_XXXX`; click it and enter PIN `123456` if prompted.
Pairing is enough — no need to "connect" from Windows settings.

**Linux:** pair once with `bluetoothctl` (`scan on` → `pair <MAC>` → PIN
`123456` → `trust <MAC>`). The meshtastic library does not pair by itself.
On Qubes OS, see **[docs/QUBES.md](docs/QUBES.md)**.

---

## Installation & Running

**Windows:** double-click `start_dashboard.bat`.
**Linux:** run `./start_dashboard.sh`.

Either launcher creates a virtual environment on first run, installs
`requirements.txt`, starts the Flask server, and serves the dashboard at
`http://127.0.0.1:5000`.

---

## Configuration

Copy the example config and edit it:

```bash
cp config.example.json config.local.json
```

```json
{
  "ble_device": "AA:BB:CC:DD:EE:FF",
  "server_host": "127.0.0.1",
  "server_port": 5000
}
```

- `ble_device` — your radio's MAC address or BLE name (`"Meshtastic_XXXX"`).
  Leave it out (or `null`) to auto-discover the first Meshtastic device; click
  **SCAN BLE** in the dashboard to list nearby radios. `config.local.json` is
  gitignored on purpose: keep your radio's MAC out of the repository.
- `server_host` — `127.0.0.1` keeps the dashboard local to this machine. Set
  `"0.0.0.0"` to reach it from other devices on your network.
- All keys are optional; defaults live in `DEFAULTS` at the top of `server.py`
  (poll timeout, scan length, retry counts, file paths).

---

## Usage Workflow

```
1. Turn off Bluetooth on your Android phone
         ↓
2. Click  ▶ POLL RADIO  in the dashboard
         ↓
3. Watch the status bar — it will show:
     "Scanning…" → "BLE connected — requesting telemetry…" → "✅ Telemetry saved"
         ↓
4. Dashboard updates with new stats and charts
```

The dashboard keeps the BLE connection open between polls (the nRF52 stops
advertising reliably otherwise). To hand the radio back to your phone, click
**DISCONNECT** in the dashboard first, then reconnect the Meshtastic app.

---

## What Each Stat Means

| Stat | Description | Healthy range |
|------|-------------|---------------|
| **ChUtil %** | Channel utilization — how busy the LoRa channel is | < 25% recommended |
| **AirUtilTX %** | Percentage of airtime your node is transmitting | < 10% |
| **Battery %** | Battery level (> 100 = USB powered) | — |
| **Voltage** | Battery voltage | 3.7–4.2 V |
| **Uptime** | Time since last reboot | Higher = more stable |
| **TX Packets** | Total packets this node transmitted | — |
| **RX Packets** | Total packets received | — |
| **RX Bad** | Corrupted packets received | Should be low |
| **RX Dupe** | Duplicate packets received | — |
| **TX Relay** | Packets relayed to mesh for others | — |
| **Relay Cancel** | Relay attempts cancelled (collision avoidance) | — |

---

## Telemetry Not Updating?

The RAK4631 broadcasts device metrics every **30 minutes** by default, and
`local_stats` (packet counters) may not be enabled. The dashboard requests
them on demand — but if the radio doesn't respond:

1. In the Meshtastic Android app, go to:
   **Settings → Modules → Telemetry**
2. Set **Device Metrics Update Interval** to 300 seconds (5 min) or less
3. Enable **Store and Forward** if you want packet counter stats

---

## Files

```
Meshtastic-Stats/
├── server.py              Main server + BLE collector + Flask dashboard
├── config.example.json    Config template — copy to config.local.json (gitignored)
├── requirements.txt       Python dependencies
├── start_dashboard.bat    Windows launcher (double-click to run)
├── start_dashboard.sh     Linux launcher
├── ble_diag.py            BLE signal-strength diagnostic (60 s listen)
├── capture_fixture.py     Capture real radio data as a local test fixture
├── docs/QUBES.md          Running on Qubes OS (NitroPad etc.)
├── tests/                 Hardware-free test suite (pytest)
└── mesh_stats.db          SQLite history (auto-created, gitignored)
```

---

## Troubleshooting

**"No Meshtastic BLE devices found"**
→ Make sure the radio is powered on and within ~10m
→ Make sure the phone's Bluetooth is off (only one BLE client at a time)
→ Make sure the device is paired to the PC first

**"BLE error: ... access denied"**
→ Unpair and re-pair the device in your Bluetooth settings

**Charts not showing / all dashes**
→ You need at least one successful poll first to populate data

**Dashboard won't open from another device**
→ By default the server binds `127.0.0.1` (this machine only). Set
`"server_host": "0.0.0.0"` in `config.local.json`, and allow the port
through your firewall.
