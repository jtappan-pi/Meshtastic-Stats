"""
Meshtastic RAK4631 Health Dashboard - server.py
================================================
Connects via BLE to the RAK4631, polls telemetry on demand,
stores history in SQLite, and serves a web dashboard.

Usage:
    python server.py

Then open: http://localhost:5000
"""

import contextlib
import datetime
import json
import logging
import os
import sqlite3
import sys
import threading
import time

from flask import Flask, jsonify, render_template_string, request

# ---------------------------------------------------------------------------
# Config — defaults below, overridden by a local JSON file (never committed:
# it holds the radio's MAC). Copy config.example.json to config.local.json.
# ---------------------------------------------------------------------------
CONFIG_ENV_VAR = "MESH_DASHBOARD_CONFIG"     # overrides the config file path
DEFAULT_CONFIG_PATH = "config.local.json"    # relative to cwd, like DB_PATH

DEFAULTS = {
    "ble_device": None,       # radio MAC or BLE name ("Meshtastic_XXXX");
                              # None auto-discovers the first Meshtastic device
    "db_path": "mesh_stats.db",
    "layout_path": "card_layout.json",   # saved card arrangement (drag-and-drop)
    "log_path": "mesh_dashboard.log",
    "server_host": "127.0.0.1",   # set "0.0.0.0" to reach the dashboard from other devices
    "server_port": 5000,
    "poll_timeout": 30,       # seconds to wait for telemetry response after connecting
    "ble_scan_seconds": 25,   # long unfiltered scan; the radio's BLE signal is marginal
    "ble_max_attempts": 6,    # connection attempts per poll (each is a coin flip at marginal RSSI)
    "ble_gatt_fail_cooldown": 75,  # s to wait after a link-dropped failure: the radio holds a
                                   # stale half-open connection and stops advertising until it times out
}


def load_config(path=None):
    """Return DEFAULTS overlaid with the local config file, if present.

    A missing file is fine (fresh clone, tests): defaults apply. A file that
    exists but is malformed or has unknown keys aborts with a clear message
    rather than silently running on defaults."""
    if path is None:
        path = os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        raise SystemExit(f"Config file {path} is unreadable or not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit(f"Config file {path} must contain a JSON object")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise SystemExit(f"Unknown key(s) in {path}: {', '.join(unknown)}")
    cfg.update(data)
    return cfg


_CFG = load_config()
DB_PATH       = _CFG["db_path"]
BLE_DEVICE    = _CFG["ble_device"]
POLL_TIMEOUT  = _CFG["poll_timeout"]
SERVER_HOST   = _CFG["server_host"]
SERVER_PORT   = _CFG["server_port"]
LAYOUT_PATH   = _CFG["layout_path"]
LOG_PATH      = _CFG["log_path"]
BLE_SCAN_SECONDS = _CFG["ble_scan_seconds"]
BLE_MAX_ATTEMPTS = _CFG["ble_max_attempts"]
BLE_GATT_FAIL_COOLDOWN = _CFG["ble_gatt_fail_cooldown"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8", delay=True),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Persistent BLE connection manager
# ---------------------------------------------------------------------------
_iface      = None   # persistent meshtastic BLE interface
_iface_lock = threading.Lock()

def _filter_scan_results(pairs, target_sanitized, service_uuid):
    """pairs: iterable of (BLEDevice, AdvertisementData).
    Keep devices that are the configured radio (matched by MAC) or that
    advertise the Meshtastic service UUID."""
    out = []
    su = service_uuid.lower()
    for d, adv in pairs:
        addr = (getattr(d, "address", "") or "").replace(":", "").replace("-", "").lower()
        uuids = [u.lower() for u in (getattr(adv, "service_uuids", None) or [])]
        if (target_sanitized and addr == target_sanitized) or su in uuids:
            out.append(d)
    return out

_scan_patched = False

def _install_long_scan():
    """Replace meshtastic's built-in BLE scan (10 s, filtered to adverts that
    carry the Meshtastic service UUID) with a longer UNFILTERED scan matched
    by MAC address. At marginal signal (about -85 dBm and weaker) Windows
    receives too few of the filtered advertisements for a 10 s window, which
    shows up as 'No Meshtastic BLE peripheral found' even though the radio is
    advertising steadily."""
    global _scan_patched
    if _scan_patched:
        return
    import meshtastic.ble_interface as mbi
    target = (BLE_DEVICE or "").replace(":", "").replace("-", "").lower()

    def long_scan():
        log.info(f"Long BLE scan: {BLE_SCAN_SECONDS}s, unfiltered, matching by address...")
        with mbi.BLEClient() as client:
            response = client.discover(timeout=BLE_SCAN_SECONDS, return_adv=True)
        devices = _filter_scan_results(response.values(), target, mbi.SERVICE_UUID)
        log.info(f"Long BLE scan done: {len(devices)} matching device(s)")
        return devices

    mbi.BLEInterface.scan = staticmethod(long_scan)
    _scan_patched = True
    log.info("BLE scan patched for marginal-signal operation")

def _get_or_connect_iface():
    """Return existing BLE interface if still alive, otherwise reconnect."""
    global _iface
    import meshtastic.ble_interface
    _install_long_scan()

    with _iface_lock:
        # Check if existing interface is still alive
        if _iface is not None:
            try:
                _ = _iface.nodes  # light liveness check
                log.info("Reusing existing BLE connection")
                return _iface
            except Exception:
                log.info("Existing BLE connection dead — reconnecting")
                try: _iface.close()
                except Exception: pass
                _iface = None

        # Connect fresh. At marginal signal each attempt is a coin flip, and the
        # failure mode decides the right wait before the next flip:
        #  - "not found": radio simply was not heard -> retry soon
        #  - link dropped during setup ("Not connected" etc.): the radio usually
        #    keeps a stale half-open connection and stops advertising until it
        #    times out, so rescanning immediately is guaranteed to fail -> wait
        #    out the stale-connection window first
        last_err = None
        time.sleep(2)
        for attempt in range(1, BLE_MAX_ATTEMPTS + 1):
            set_status("connecting",
                       f"BLE connect attempt {attempt}/{BLE_MAX_ATTEMPTS} "
                       f"(scan takes ~{BLE_SCAN_SECONDS}s)…")
            try:
                log.info(f"BLE connect attempt {attempt}...")
                _iface = meshtastic.ble_interface.BLEInterface(BLE_DEVICE)
                log.info("BLE connected successfully — holding this connection")
                return _iface
            except Exception as e:
                last_err = e
                _iface = None
                log.warning(f"BLE connect attempt {attempt} failed: {e}")
                if attempt >= BLE_MAX_ATTEMPTS:
                    break
                low = str(e).lower()
                if "peripheral" in low or "not found" in low:   # scan simply did not hear it
                    wait, note = 10, "radio not heard"
                else:
                    wait, note = BLE_GATT_FAIL_COOLDOWN,                         "link dropped during setup; waiting out the radio's stale-connection window"
                set_status("connecting",
                           f"Attempt {attempt}/{BLE_MAX_ATTEMPTS} failed ({note}) — next try in {wait}s…")
                time.sleep(wait)

        raise Exception(
            f"Failed after {BLE_MAX_ATTEMPTS} attempts. Last error: {last_err}. "
            f"Signal to the radio is marginal, so each attempt is a coin flip — "
            f"polling again often succeeds. The durable fix is more signal "
            f"(USB Bluetooth dongle on an extension cable, or reposition radio/PC)."
        )

def _release_iface():
    """Close and release the persistent interface."""
    global _iface
    with _iface_lock:
        if _iface is not None:
            try: _iface.close()
            except Exception as e: log.warning(f"Close error: {e}")
            _iface = None
            log.info("BLE interface released")

# Global state
# ---------------------------------------------------------------------------
_status = {
    "state":        "idle",    # idle | connecting | polling | disconnecting | error
    "message":      "Ready. Press 'Poll Radio' to connect and fetch telemetry.",
    "last_polled":  None,
    "ble_address":  None,
}
_status_lock = threading.Lock()

def set_status(state, message, ble_address=None):
    with _status_lock:
        _status["state"]   = state
        _status["message"] = message
        if ble_address:
            _status["ble_address"] = ble_address
    log.info(f"[{state}] {message}")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()   # one DB user at a time inside this process (see _db_target)

def _db_target(db_path, windows=None):
    """Return (connect_arg, uri_flag) for sqlite3.connect().

    On Windows this folder is normally reached over a \\wsl.localhost\\ UNC path, a 9P
    network share that rejects the byte-range locks SQLite's default Windows VFS takes.
    That surfaces as 'database is locked' on the very first statement. Only this
    process ever writes the file, and _db_lock serializes access inside it, so on
    Windows we use SQLite's lock-free VFS instead. SQLite URIs cannot carry a host,
    so a UNC db_path falls back to a plain open (and will hit the lock error)."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return db_path, False
    path = db_path.replace("\\", "/")
    if path.startswith("//"):                       # \\server\share\... : no URI form
        return db_path, False
    if len(path) > 1 and path[1] == ":":            # C:/x -> /C:/x
        path = "/" + path
    return f"file:{path}?vfs=win32-none", True

@contextlib.contextmanager
def _db():
    """Open the telemetry DB, serialized, always closed."""
    with _db_lock:
        target, uri = _db_target(DB_PATH)
        conn = sqlite3.connect(target, uri=uri)
        try:
            yield conn
        finally:
            conn.close()

def init_db():
    with _db() as conn:
        _create_schema(conn)
    log.info(f"Database ready at {DB_PATH}")

def _create_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT    NOT NULL,
            -- Device metrics
            ch_util          REAL,
            air_util_tx      REAL,
            battery          INTEGER,
            voltage          REAL,
            uptime_sec       INTEGER,
            -- Local stats (packet counters)
            tx_count         INTEGER,
            rx_count         INTEGER,
            rx_bad           INTEGER,
            rx_dupe          INTEGER,
            tx_relay         INTEGER,
            tx_relay_cancel  INTEGER,
            -- Heap memory
            heap_total       INTEGER,
            heap_free        INTEGER,
            -- Online nodes
            num_online_nodes INTEGER,
            num_total_nodes  INTEGER,
            -- RF environment
            noise_floor      INTEGER,
            tx_dropped       INTEGER,
            -- Source info
            node_id          TEXT
        )
    """)
    # Add new columns to existing DBs gracefully
    for col, typ in [
        ("heap_total",       "INTEGER"),
        ("heap_free",        "INTEGER"),
        ("num_online_nodes", "INTEGER"),
        ("num_total_nodes",  "INTEGER"),
        ("noise_floor",      "INTEGER"),
        ("tx_dropped",       "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE telemetry ADD COLUMN {col} {typ}")
        except Exception:
            pass  # column already exists
    conn.commit()

# ---------------------------------------------------------------------------
# Parsing: radio data -> telemetry row. Pure functions, no hardware, no I/O.
# These take the dicts the meshtastic library produces (camelCase keys) and are
# what the fixture-based tests exercise.
# ---------------------------------------------------------------------------
def utc_now_iso():
    """Timestamp in the exact format stored since v1: 2026-10-19T00:57:25Z.
    The dashboard JS slices this string by position, so keep the shape."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def select_local_node(my_num, nodes):
    """Pick this radio's entry out of the node DB (iface.nodes: {"!id": node_dict}).

    Returns (node_id, deviceMetrics). If the local node is not found or has no
    deviceMetrics, falls back to the first node in the DB (both id and metrics).
    Returns ("local", {}) when the DB is empty."""
    nodes = nodes or {}
    node_id, dm = "local", {}
    if my_num:
        for nid, node in nodes.items():
            if node.get("num") == my_num:
                node_id, dm = nid, node.get("deviceMetrics", {})
                log.info(f"Read deviceMetrics [{nid}]: {dm}")
                break
    if not dm and nodes:
        node_id, node = next(iter(nodes.items()))
        dm = node.get("deviceMetrics", {})
        log.info(f"Fallback deviceMetrics [{node_id}]: {dm}")
    return node_id, dm

def extract_local_stats(packet):
    """Return the localStats dict from a decoded telemetry packet, or {} if absent."""
    tel = (packet.get("decoded") or {}).get("telemetry") or {}
    return tel.get("localStats") or {}

ROW_COLUMNS = (
    "ts", "ch_util", "air_util_tx", "battery", "voltage", "uptime_sec",
    "tx_count", "rx_count", "rx_bad", "rx_dupe", "tx_relay", "tx_relay_cancel",
    "heap_total", "heap_free", "num_online_nodes", "num_total_nodes",
    "noise_floor", "tx_dropped", "node_id",
)

def build_row(dm: dict, ls: dict, node_id: str = "local", ts: str = None) -> dict:
    """Map deviceMetrics (dm) and localStats (ls) onto one telemetry row.
    Packet counters default to 0 when localStats never arrived; everything
    else stays NULL."""
    return {
        "ts":               ts or utc_now_iso(),
        "ch_util":          dm.get("channelUtilization"),
        "air_util_tx":      dm.get("airUtilTx"),
        "battery":          dm.get("batteryLevel"),
        "voltage":          dm.get("voltage"),
        "uptime_sec":       dm.get("uptimeSeconds"),
        "tx_count":         ls.get("numPacketsTx", 0),
        "rx_count":         ls.get("numPacketsRx", 0),
        "rx_bad":           ls.get("numPacketsRxBad", 0),
        "rx_dupe":          ls.get("numRxDupe", 0),
        "tx_relay":         ls.get("numTxRelay", 0),
        "tx_relay_cancel":  ls.get("numTxRelayCanceled", 0),
        "heap_total":       ls.get("heapTotalBytes"),
        "heap_free":        ls.get("heapFreeBytes"),
        "num_online_nodes": ls.get("numOnlineNodes"),
        "num_total_nodes":  ls.get("numTotalNodes"),
        "noise_floor":      ls.get("noiseFloor"),
        "tx_dropped":       ls.get("numTxDropped", 0),
        "node_id":          node_id,
    }

def save_telemetry(dm: dict, ls: dict, node_id: str = "local"):
    row = build_row(dm, ls, node_id)
    with _db() as conn:
        conn.execute(
            f"INSERT INTO telemetry ({', '.join(ROW_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(ROW_COLUMNS))})",
            tuple(row[c] for c in ROW_COLUMNS),
        )
        conn.commit()
    log.info(f"Saved telemetry row @ {row['ts']} | ChUtil={row['ch_util']} AirUtilTX={row['air_util_tx']} "
             f"Battery={row['battery']}% Noise={row['noise_floor']}dBm")
    return row["ts"]

def query_latest():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM telemetry ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {}

def query_history(limit=300):
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]  # chronological order

# ---------------------------------------------------------------------------
# BLE Polling — runs in a background thread when triggered
# ---------------------------------------------------------------------------
def _ble_poll_thread():
    """
    Uses a persistent BLE connection — connects once and reuses the connection
    on subsequent polls so the radio stays awake between polls.
    """
    try:
        from pubsub import pub
    except ImportError as e:
        set_status("error", f"Import error: {e}. Run: pip install meshtastic")
        return

    received_ls = {}

    def on_receive(packet, interface):
        ls = extract_local_stats(packet)
        if ls:
            received_ls.update(ls)
            log.info(f"Got localStats packet: {ls}")

    try:
        set_status("connecting", "Connecting to Meshtastic BLE device…")
        pub.subscribe(on_receive, "meshtastic.receive.telemetry")

        iface = _get_or_connect_iface()

        set_status("polling", "BLE connected — reading node data…")

        # Read deviceMetrics from node DB
        time.sleep(3)
        my_num = iface.myInfo.my_node_num if iface.myInfo else None
        node_id, dm = select_local_node(my_num, iface.nodes)

        # Request local_stats
        try:
            dest_id = node_id if node_id != "local" else "^all"
            iface.sendTelemetry(telemetryType="local_stats", destinationId=dest_id, wantResponse=True)
            log.info(f"Requested local_stats from {dest_id}")
        except Exception as e:
            log.warning(f"local_stats request error (non-fatal): {e}")

        deadline = time.time() + 20
        while not received_ls and time.time() < deadline:
            time.sleep(0.5)

        if received_ls:
            log.info(f"Got local_stats: {received_ls}")
        else:
            log.info("No local_stats received within timeout")

        if dm:
            ts = save_telemetry(dm, received_ls, node_id)
            with _status_lock:
                _status["last_polled"] = ts
            set_status("idle", f"✅ Telemetry saved at {ts} UTC — ready for next poll.")
        else:
            set_status("error", "⚠️ Connected but no deviceMetrics in node DB. Try polling again.")

    except Exception as e:
        set_status("error", f"❌ BLE error: {e}")
        log.exception("BLE poll failed")
        # Release broken connection so next poll reconnects fresh
        _release_iface()

    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive.telemetry")
        except Exception:
            pass
        # NOTE: We intentionally do NOT close the interface here.
        # Keeping the BLE connection alive prevents the radio from stopping
        # BLE advertising between polls (nRF52 wait_bluetooth_secs timer).
        with _status_lock:
            if _status["state"] not in ("idle", "error"):
                _status["state"] = "idle"


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)

# --- Dashboard HTML (single-file, no external template files needed) ---
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAK4631 · Health Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:       #0a0e14;
    --surface:  #111720;
    --border:   #1e2d3d;
    --accent:   #00d4ff;
    --accent2:  #ff6b35;
    --accent3:  #39ff14;
    --warn:     #ffcc00;
    --text:     #c8d8e8;
    --dim:      #4a6580;
    --font-mono: 'Share Tech Mono', monospace;
    --font-body: 'Barlow', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Animated grid background */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(0,212,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,212,255,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }

  .wrap {
    position: relative;
    z-index: 1;
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px 20px 48px;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    margin-bottom: 28px;
    gap: 16px;
    flex-wrap: wrap;
  }
  .header-left { display: flex; align-items: center; gap: 14px; }
  .logo-icon {
    width: 42px; height: 42px;
    border: 1px solid var(--accent);
    border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
    box-shadow: 0 0 12px rgba(0,212,255,0.3);
  }
  h1 {
    font-family: var(--font-mono);
    font-size: 1.25rem;
    color: var(--accent);
    letter-spacing: 0.08em;
    text-shadow: 0 0 16px rgba(0,212,255,0.5);
  }
  h1 span { color: var(--dim); font-size: 0.75rem; display: block; margin-top: 2px; letter-spacing: 0.12em; }

  /* ── Status Bar ── */
  #status-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 24px;
    font-family: var(--font-mono);
    font-size: 0.82rem;
    min-height: 46px;
    transition: border-color 0.3s;
  }
  #status-bar.state-idle        { border-color: var(--dim); }
  #status-bar.state-connecting  { border-color: var(--warn); }
  #status-bar.state-polling     { border-color: var(--accent); }
  #status-bar.state-disconnecting { border-color: var(--accent2); }
  #status-bar.state-error       { border-color: #ff4444; }

  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
    transition: background 0.3s;
  }
  .state-idle         .status-dot { background: var(--dim); }
  .state-connecting   .status-dot { background: var(--warn);   animation: pulse 1s infinite; }
  .state-polling      .status-dot { background: var(--accent); animation: pulse 0.6s infinite; }
  .state-disconnecting .status-dot { background: var(--accent2); }
  .state-error        .status-dot { background: #ff4444; }

  @keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.4; transform: scale(1.4); }
  }

  #status-msg { flex: 1; color: var(--text); }

  /* ── Poll Button ── */
  .poll-btn {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    letter-spacing: 0.08em;
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 4px;
    padding: 8px 20px;
    cursor: pointer;
    transition: background 0.2s, box-shadow 0.2s, opacity 0.2s;
    white-space: nowrap;
  }
  .poll-btn:hover:not(:disabled) {
    background: rgba(0,212,255,0.12);
    box-shadow: 0 0 16px rgba(0,212,255,0.35);
  }
  .poll-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* ── Stat Cards ── */
  .cards {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.3s;
  }
  .card::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 2px;
    background: var(--card-accent, var(--accent));
    opacity: 0.6;
  }
  .card-label {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    color: var(--dim);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .card-value {
    font-family: var(--font-mono);
    font-size: 1.8rem;
    color: var(--card-accent, var(--accent));
    line-height: 1;
    text-shadow: 0 0 12px var(--card-accent, var(--accent));
  }
  .card-unit {
    font-size: 0.75rem;
    color: var(--dim);
    margin-top: 4px;
    font-family: var(--font-mono);
  }
  @media (max-width: 900px) { .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  .slot {
    min-height: 104px;
    border-radius: 8px;
    border: 1px dashed rgba(74,101,128,0.25);
    transition: border-color 0.15s, background 0.15s;
  }
  .slot.filled { border-color: transparent; }
  .slot.over   { border-color: var(--accent); background: rgba(0,212,255,0.06); }
  .slot > .card { height: 100%; cursor: grab; user-select: none; }
  .slot > .card.dragging { opacity: 0.35; }
  .cards-toolbar {
    display: flex; justify-content: space-between; align-items: center;
    font-family: var(--font-mono); font-size: 0.65rem; color: var(--dim);
    letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 10px;
  }
  .link-btn {
    font: inherit; color: var(--dim); background: none; border: none;
    cursor: pointer; letter-spacing: inherit; text-transform: inherit;
  }
  .link-btn:hover { color: var(--accent); }
  .derived-card {
    border-style: dashed;
    border-width: 1px;
  }
  .derived-card::before {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(135deg, rgba(255,255,255,0.02) 0%, transparent 60%);
    pointer-events: none;
  }

  /* ── Tooltip ── */
  #tooltip {
    position: fixed;
    z-index: 50;
    max-width: 290px;
    background: #0d1420;
    border: 1px solid var(--accent);
    border-radius: 6px;
    padding: 10px 12px;
    font-family: var(--font-body);
    font-size: 0.78rem;
    line-height: 1.45;
    color: var(--text);
    box-shadow: 0 4px 24px rgba(0,0,0,0.6);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.12s;
  }
  #tooltip.show { opacity: 1; }

  /* ── Charts ── */
  .charts-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 28px;
  }
  @media (max-width: 700px) { .charts-grid { grid-template-columns: 1fr; } }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }
  .chart-title {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 12px;
  }
  .chart-wrap { position: relative; height: 160px; }

  /* ── Table ── */
  .table-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    overflow-x: auto;
  }
  .table-title {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 12px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--font-mono);
    font-size: 0.75rem;
  }
  th {
    text-align: left;
    color: var(--dim);
    font-weight: 400;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td {
    padding: 6px 10px;
    color: var(--text);
    border-bottom: 1px solid rgba(30,45,61,0.5);
    white-space: nowrap;
  }
  tr:hover td { background: rgba(0,212,255,0.04); }
  td.num { color: var(--accent); }

  /* ── Footer ── */
  footer {
    margin-top: 32px;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    color: var(--dim);
    text-align: center;
    letter-spacing: 0.1em;
  }

  /* Scan results */
  #scan-result {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--accent3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 20px;
    display: none;
  }
</style>
</head>
<body>
<div class="wrap">

  <!-- Header -->
  <header>
    <div class="header-left">
      <div class="logo-icon">📡</div>
      <h1>RAK4631 HEALTH<span>WISMESH POCKET v2 · 915 MHz · LoRa</span></h1>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="poll-btn" id="scan-btn" onclick="doScan()">⟳ SCAN BLE</button>
      <button class="poll-btn" id="poll-btn" onclick="doPoll()">▶ POLL RADIO</button>
      <button class="poll-btn" id="disc-btn" onclick="doDisconnect()" style="border-color:#ff6b35;color:#ff6b35" title="Release BLE so Android app can reconnect">⏹ DISCONNECT</button>
    </div>
  </header>

  <!-- Status Bar -->
  <div id="status-bar" class="state-idle">
    <div class="status-dot"></div>
    <span id="status-msg">Ready. Press POLL RADIO to connect and fetch telemetry.</span>
  </div>

  <!-- Scan results -->
  <div id="scan-result"></div>

  <!-- Stat Cards -->
  <div class="cards-toolbar">
    <span>Drag cards to rearrange · drop on a card to swap</span>
    <button class="link-btn" onclick="resetLayout()">↺ Reset layout</button>
  </div>
  <div class="cards" id="cards">
    <!-- injected by JS -->
  </div>

  <!-- Charts -->
  <div class="charts-grid">
    <div class="chart-card">
      <div class="chart-title">Channel Utilization %</div>
      <div class="chart-wrap"><canvas id="chart-chutil"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Air Util TX %</div>
      <div class="chart-wrap"><canvas id="chart-airutil"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Packets TX / RX</div>
      <div class="chart-wrap"><canvas id="chart-packets"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Battery Level %</div>
      <div class="chart-wrap"><canvas id="chart-battery"></canvas></div>
    </div>
    <div class="chart-card" style="grid-column:1 / -1">
      <div class="chart-title">Noise Floor dBm (lower = quieter)</div>
      <div class="chart-wrap"><canvas id="chart-noise"></canvas></div>
    </div>
  </div>

  <!-- History Table -->
  <div class="table-card">
    <div class="table-title">Poll History (latest 50)</div>
    <table>
      <thead>
        <tr>
          <th>Timestamp (UTC)</th>
          <th>ChUtil %</th>
          <th>AirUtilTX %</th>
          <th>Battery %</th>
          <th>Voltage</th>
          <th>Uptime</th>
          <th>TX</th>
          <th>RX</th>
          <th>RX Bad</th>
          <th>RX Dupe</th>
          <th>TX Relay</th>
          <th>Relay Cancel</th>
          <th>TX Dropped</th>
          <th>Noise dBm</th>
          <th>Heap Free</th>
          <th>Heap Total</th>
          <th>Online</th>
          <th>Total Nodes</th>
          <th>Relay Success %</th>
          <th>RX Quality %</th>
          <th>Dupe Rate %</th>
        </tr>
      </thead>
      <tbody id="history-tbody"></tbody>
    </table>
  </div>

  <footer>RAK4631 · MESHTASTIC · Dashboard v1.0 · data stored locally in mesh_stats.db</footer>
</div>

<script>
// ── Chart instances ──
const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 400 },
  plugins: { legend: { display: false } },
  scales: {
    x: { ticks: { color: '#4a6580', font: { family: 'Share Tech Mono', size: 10 }, maxTicksLimit: 8 },
         grid: { color: 'rgba(30,45,61,0.8)' } },
    y: { ticks: { color: '#4a6580', font: { family: 'Share Tech Mono', size: 10 } },
         grid: { color: 'rgba(30,45,61,0.8)' } }
  }
};

function mkChart(id, color, fill=false) {
  const ctx = document.getElementById(id).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{
      data: [], borderColor: color, backgroundColor: fill ? color+'22' : 'transparent',
      tension: 0.35, pointRadius: 3, pointBackgroundColor: color, fill
    }]},
    options: JSON.parse(JSON.stringify(CHART_DEFAULTS))
  });
}

const charts = {
  chutil:  mkChart('chart-chutil',  '#00d4ff', true),
  airutil: mkChart('chart-airutil', '#ff6b35', true),
  packets: null,   // two-dataset, built separately
  battery: mkChart('chart-battery', '#39ff14', true),
  noise:   mkChart('chart-noise',   '#c77dff', false),
};

// Packets chart (TX + RX)
(function() {
  const ctx = document.getElementById('chart-packets').getContext('2d');
  charts.packets = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'TX', data: [], borderColor: '#00d4ff', backgroundColor: 'transparent',
        tension: 0.35, pointRadius: 2 },
      { label: 'RX', data: [], borderColor: '#39ff14', backgroundColor: 'transparent',
        tension: 0.35, pointRadius: 2 },
    ]},
    options: {
      ...JSON.parse(JSON.stringify(CHART_DEFAULTS)),
      plugins: { legend: { display: true, labels: { color: '#4a6580',
        font: { family: 'Share Tech Mono', size: 10 } } } }
    }
  });
})();

// Noise floor: lower (more negative) = quieter RF environment
function noiseAccent(v) {
  if (v == null) return '#4a6580';
  if (v <= -110) return '#39ff14';   // quiet
  if (v <= -100) return '#ffcc00';   // moderate interference
  return '#ff4444';                  // noisy — look for nearby interference
}

// ── Tooltips: what each card means and how to read it ──
// (plain text only - no quotes, rendered into data-tip attributes)
const TIPS = {
  ch_util:          'How busy the LoRa channel is with traffic from ALL nodes, not just yours. Under 25% is healthy; sustained values above 40% mean a congested mesh.',
  air_util_tx:      'Share of airtime this node spends transmitting. Keep it under 10% to be a good mesh citizen.',
  battery:          'Battery charge level. 101% means the radio is running on USB power.',
  voltage:          'Battery voltage. A healthy lithium cell sits between about 3.7 and 4.2 V.',
  uptime_sec:       'Time since the radio last rebooted. If this drops unexpectedly between polls, the radio crashed or lost power.',
  tx_count:         'Total packets transmitted since boot, including relays for other nodes. Raw counter - it grows with uptime, so judge trends via the ratio cards.',
  rx_count:         'Total packets received since boot from all nodes in range. Raw counter - grows with uptime.',
  rx_bad:           'Packets that arrived corrupted and were discarded. Judge it against RX Packets via the RX Quality card rather than on its own.',
  rx_dupe:          'Packets heard more than once because several neighbors relayed the same thing. Some duplication is normal mesh flooding.',
  tx_relay:         'Packets this node forwarded on behalf of other nodes - the core measure of how much it serves the mesh.',
  tx_relay_cancel:  'Relays this node skipped because a neighbor relayed the packet first. Normal collision avoidance, not a failure.',
  tx_dropped:       'Packets the radio gave up sending, usually because the channel stayed busy. Should stay at or near zero.',
  noise_floor:      'Background RF noise where the radio listens. More negative is quieter: green at -110 dBm or below; above -100 suggests nearby interference.',
  heap_free:        'Free working memory on the radio. Below roughly 20% of Heap Total risks instability.',
  heap_total:       'Total working memory the firmware has. Fixed for a given firmware version.',
  num_online_nodes: 'Nodes heard recently enough to be considered online right now.',
  num_total_nodes:  'All nodes this radio has ever recorded in its node database.',
  relay_success_pct:'Relays sent / (relays sent + relays skipped). High means this node usually does the relaying before any neighbor beats it to it - a sign it is a key relay in the area. Green at 85%+.',
  relay_share_pct:  'Share of ALL transmissions that were relays for other nodes. High means most airtime is spent serving the mesh rather than sending its own traffic. Informational - no good or bad threshold.',
  rx_quality_pct:   'Clean packets as a share of everything received: (RX - RX Bad) / RX. Green at 80%+. Low values point to weak signals or collisions - antenna height and placement matter most.',
  dupe_pct:         'Duplicates as a share of everything received: RX Dupe / RX. Green at 15% or less. High values suggest many neighbors relaying the same traffic.',
  d_relay_share_pct:'Relay Share counted only between the last two polls - how much of RECENT airtime went to relaying for others, instead of an average over the whole uptime.',
  d_rx_quality_pct: 'RX Quality counted only between the last two polls, so an antenna or placement change shows up immediately instead of being buried in since-boot totals.',
  d_dupe_pct:       'Dupe Rate counted only between the last two polls. Useful for spotting recent changes in nearby relay behavior.',
};

const DERIVED_DEFS = [
  { key: 'relay_success_pct', label: 'Relay Success' },
  { key: 'relay_share_pct',   label: 'Relay Share' },
  { key: 'rx_quality_pct',    label: 'RX Quality' },
  { key: 'dupe_pct',          label: 'Dupe Rate' },
  { key: 'd_relay_share_pct', label: 'Relay Share Δ' },
  { key: 'd_rx_quality_pct',  label: 'RX Quality Δ' },
  { key: 'd_dupe_pct',        label: 'Dupe Rate Δ' },
];

// ── Card definitions ──
const CARD_DEFS = [
  { key: 'ch_util',         label: 'ChUtil',        unit: '%',    accent: '#00d4ff' },
  { key: 'air_util_tx',     label: 'AirUtilTX',     unit: '%',    accent: '#ff6b35' },
  { key: 'battery',         label: 'Battery',       unit: '%',    accent: '#39ff14' },
  { key: 'voltage',         label: 'Voltage',       unit: 'V',    accent: '#ffcc00', decimals: 2 },
  { key: 'uptime_sec',      label: 'Uptime',        unit: '',     accent: '#c77dff', fmt: fmtUptime },
  { key: 'tx_count',        label: 'TX Packets',    unit: 'pkts', accent: '#00d4ff' },
  { key: 'rx_count',        label: 'RX Packets',    unit: 'pkts', accent: '#39ff14' },
  { key: 'rx_bad',          label: 'RX Bad',        unit: 'pkts', accent: '#ff4444' },
  { key: 'rx_dupe',         label: 'RX Dupe',       unit: 'pkts', accent: '#ff6b35' },
  { key: 'tx_relay',        label: 'TX Relay',      unit: 'pkts', accent: '#00d4ff' },
  { key: 'tx_relay_cancel', label: 'Relay Cancel',  unit: 'pkts', accent: '#ffcc00' },
  { key: 'tx_dropped',      label: 'TX Dropped',    unit: 'pkts', accent: '#ff4444' },
  { key: 'noise_floor',     label: 'Noise Floor',   unit: 'dBm',  accent: noiseAccent },
  { key: 'heap_free',       label: 'Heap Free',     unit: 'B',    accent: '#39ff14', fmt: fmtBytes },
  { key: 'heap_total',      label: 'Heap Total',    unit: 'B',    accent: '#4a6580', fmt: fmtBytes },
  { key: 'num_online_nodes',label: 'Online Nodes',  unit: 'nodes',accent: '#00d4ff' },
  { key: 'num_total_nodes', label: 'Total Nodes',   unit: 'nodes',accent: '#4a6580' },
];

function fmtUptime(sec) {
  if (sec == null) return '--';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}
function fmtBytes(b) {
  if (b == null) return '--';
  if (b >= 1024) return (b/1024).toFixed(1) + 'K';
  return b + 'B';
}

function fmtVal(def, val) {
  if (val == null || val === '') return '--';
  if (def.fmt) return def.fmt(val);
  if (def.decimals != null) return parseFloat(val).toFixed(def.decimals);
  return val;
}

// ── Derived metrics ──
function pct(num, den) {
  return (num != null && den > 0) ? num / den * 100 : null;
}

// Returns { key: { v: percent|null, sub: subtitle } } for every derived card.
// prev = second-most-recent poll row, used for the since-last-poll (Δ) cards.
function computeDerived(r, prev) {
  const out = {};
  const mk = (k, v, sub) => { out[k] = { v, sub }; };

  // Totals since boot
  mk('relay_success_pct',
     pct(r.tx_relay, (r.tx_relay || 0) + (r.tx_relay_cancel || 0)),
     r.tx_relay != null ? `${r.tx_relay} relayed, ${r.tx_relay_cancel || 0} skipped` : '');
  mk('relay_share_pct',
     pct(r.tx_relay, r.tx_count),
     r.tx_count > 0 ? `${r.tx_relay || 0} of ${r.tx_count} tx were relays` : '');
  mk('rx_quality_pct',
     r.rx_count > 0 ? pct(r.rx_count - (r.rx_bad || 0), r.rx_count) : null,
     r.rx_count > 0 ? `${r.rx_count - (r.rx_bad || 0)} good of ${r.rx_count} rx` : '');
  mk('dupe_pct',
     r.rx_count > 0 ? pct(r.rx_dupe || 0, r.rx_count) : null,
     r.rx_count > 0 ? `${r.rx_dupe || 0} dupes of ${r.rx_count} rx` : '');

  // Since last poll (difference between the two most recent polls)
  const DELTA_KEYS = ['d_relay_share_pct', 'd_rx_quality_pct', 'd_dupe_pct'];
  const noDelta = why => DELTA_KEYS.forEach(k => mk(k, null, why));
  const rebooted = prev && r.uptime_sec != null && prev.uptime_sec != null
                   && r.uptime_sec < prev.uptime_sec;
  const d = k => (prev && r[k] != null && prev[k] != null) ? r[k] - prev[k] : null;
  const dRx = d('rx_count'), dBad = d('rx_bad'), dDupe = d('rx_dupe'),
        dTx = d('tx_count'), dRel = d('tx_relay');

  if (!prev)                                          noDelta('need two polls');
  else if (rebooted)                                  noDelta('radio rebooted between polls');
  else if ([dRx, dTx].some(x => x == null || x < 0))  noDelta('no comparable data');
  else {
    mk('d_relay_share_pct', dTx > 0 ? pct(dRel || 0, dTx) : null,
       dTx > 0 ? `${dRel || 0} of ${dTx} new tx were relays` : 'no new tx since last poll');
    mk('d_rx_quality_pct', dRx > 0 ? pct(dRx - (dBad || 0), dRx) : null,
       dRx > 0 ? `${dRx - (dBad || 0)} good of ${dRx} new rx` : 'no new rx since last poll');
    mk('d_dupe_pct', dRx > 0 ? pct(dDupe || 0, dRx) : null,
       dRx > 0 ? `${dDupe || 0} dupes of ${dRx} new rx` : 'no new rx since last poll');
  }
  return out;
}

// Health color thresholds for derived metrics (Δ cards share their parent thresholds)
function derivedAccent(key, val) {
  if (val == null) return '#4a6580';
  const v = parseFloat(val);
  const base = key.replace(/^d_/, '');
  if (base === 'relay_success_pct') {
    if (v >= 85) return '#39ff14';
    if (v >= 65) return '#ffcc00';
    return '#ff4444';
  }
  if (base === 'rx_quality_pct') {
    if (v >= 80) return '#39ff14';
    if (v >= 60) return '#ffcc00';
    return '#ff4444';
  }
  if (base === 'dupe_pct') {
    if (v <= 15) return '#39ff14';
    if (v <= 30) return '#ffcc00';
    return '#ff4444';
  }
  return '#00d4ff';   // relay_share: informational, no good/bad threshold
}

// ── Card layout (drag-and-drop) ──
const SLOT_COLS = 6;
const MIN_SLOTS = 24;
let LAYOUT = null;     // array of card keys / null (empty slot), by slot index
let _latest = {};
let _prev   = null;   // second-most-recent poll row, for the Δ cards

function normalizeLayout(layout, keys) {
  const seen = new Set();
  let out = (Array.isArray(layout) ? layout : []).map(k =>
    (k && keys.includes(k) && !seen.has(k)) ? (seen.add(k), k) : null);
  // Place any card not yet in the layout (e.g. a newly added stat) in the first empty slot
  keys.forEach(k => {
    if (seen.has(k)) return;
    const i = out.indexOf(null);
    if (i === -1) out.push(k); else out[i] = k;
    seen.add(k);
  });
  while (out.length && out[out.length - 1] === null) out.pop();
  const total = Math.max(MIN_SLOTS, Math.ceil(out.length / SLOT_COLS) * SLOT_COLS);
  while (out.length < total) out.push(null);
  if (!out.includes(null))                      // grid full: add a spare row to drag into
    for (let i = 0; i < SLOT_COLS; i++) out.push(null);
  return out;
}

function saveLayout() {
  fetch('/api/layout', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ layout: LAYOUT }) }).catch(() => {});
}

function resetLayout() {
  LAYOUT = null;
  fetch('/api/layout', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ layout: null }) }).catch(() => {});
  renderCards(_latest);
}

function renderCards(latest) {
  _latest = latest;
  const c = document.getElementById('cards');
  const derived = computeDerived(latest, _prev);

  const html = {};
  CARD_DEFS.forEach(d => {
    const accent = typeof d.accent === 'function' ? d.accent(latest[d.key]) : d.accent;
    html[d.key] = `
    <div class="card" draggable="true" data-key="${d.key}" data-tip="${TIPS[d.key] || ''}" style="--card-accent:${accent}">
      <div class="card-label">${d.label}</div>
      <div class="card-value">${fmtVal(d, latest[d.key])}</div>
      <div class="card-unit">${d.key === 'uptime_sec' ? '' : d.unit}</div>
    </div>`;
  });
  DERIVED_DEFS.forEach(d => {
    const e = derived[d.key] || { v: null, sub: '' };
    const accent = derivedAccent(d.key, e.v);
    html[d.key] = `
    <div class="card derived-card" draggable="true" data-key="${d.key}" data-tip="${TIPS[d.key] || ''}" style="--card-accent:${accent}">
      <div class="card-label">⟳ ${d.label}</div>
      <div class="card-value" style="font-size:1.6rem">${e.v != null ? e.v.toFixed(1) : '--'}${e.v != null ? '<span style="font-size:0.9rem">%</span>' : ''}</div>
      <div class="card-unit" style="color:${accent};opacity:0.8;font-size:0.68rem">${e.sub}</div>
    </div>`;
  });

  LAYOUT = normalizeLayout(LAYOUT, Object.keys(html));
  c.innerHTML = LAYOUT.map((k, i) =>
    `<div class="slot${k ? ' filled' : ''}" data-slot="${i}">${k ? html[k] : ''}</div>`
  ).join('');
}

// Drag-and-drop wiring (event delegation, set up once)
(function initDragDrop() {
  const grid = document.getElementById('cards');
  let fromSlot = null;
  const slotOf = el => el && el.closest('.slot');

  grid.addEventListener('dragstart', e => {
    const card = e.target.closest('.card');
    if (!card) return;
    fromSlot = +slotOf(card).dataset.slot;
    card.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', card.dataset.key);   // required by Firefox
  });
  grid.addEventListener('dragend', () => {
    fromSlot = null;
    grid.querySelectorAll('.dragging, .over').forEach(el => el.classList.remove('dragging', 'over'));
  });
  grid.addEventListener('dragover', e => {
    const slot = slotOf(e.target);
    if (!slot || fromSlot === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    grid.querySelectorAll('.over').forEach(el => el !== slot && el.classList.remove('over'));
    slot.classList.add('over');
  });
  grid.addEventListener('dragleave', e => {
    const slot = slotOf(e.target);
    if (slot && !slot.contains(e.relatedTarget)) slot.classList.remove('over');
  });
  grid.addEventListener('drop', e => {
    const slot = slotOf(e.target);
    if (!slot || fromSlot === null) return;
    e.preventDefault();
    const to = +slot.dataset.slot;
    if (to !== fromSlot) {
      [LAYOUT[fromSlot], LAYOUT[to]] = [LAYOUT[to], LAYOUT[fromSlot]];   // move or swap
      saveLayout();
    }
    fromSlot = null;
    renderCards(_latest);
  });
})();

// Tooltip wiring: one fixed element, repositioned per hovered card
(function initTooltips() {
  const grid = document.getElementById('cards');
  const tip = document.createElement('div');
  tip.id = 'tooltip';
  document.body.appendChild(tip);
  let cur = null;
  const hide = () => { cur = null; tip.classList.remove('show'); };
  grid.addEventListener('mouseover', e => {
    const card = e.target.closest('.card');
    if (!card || !card.dataset.tip) { hide(); return; }
    if (card === cur) return;
    cur = card;
    tip.textContent = card.dataset.tip;
    tip.classList.add('show');
    const r = card.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    const x = Math.min(Math.max(8, r.left), window.innerWidth - tw - 8);
    let y = r.bottom + 8;
    if (y + th > window.innerHeight - 8) y = r.top - th - 8;
    tip.style.left = x + 'px';
    tip.style.top  = y + 'px';
  });
  grid.addEventListener('mouseleave', hide);
  grid.addEventListener('dragstart', hide);
  grid.addEventListener('scroll', hide);
  document.addEventListener('scroll', hide, { passive: true });
})();

function renderHistory(rows) {
  const tbody = document.getElementById('history-tbody');
  const recent = rows.slice(-50).reverse();
  tbody.innerHTML = recent.map(r => `
    <tr>
      <td>${r.ts || '--'}</td>
      <td class="num">${r.ch_util != null ? r.ch_util.toFixed(1) : '--'}</td>
      <td class="num">${r.air_util_tx != null ? r.air_util_tx.toFixed(1) : '--'}</td>
      <td class="num">${r.battery ?? '--'}</td>
      <td class="num">${r.voltage != null ? r.voltage.toFixed(2) : '--'}</td>
      <td>${r.uptime_sec != null ? fmtUptime(r.uptime_sec) : '--'}</td>
      <td class="num">${r.tx_count ?? '--'}</td>
      <td class="num">${r.rx_count ?? '--'}</td>
      <td class="num">${r.rx_bad ?? '--'}</td>
      <td class="num">${r.rx_dupe ?? '--'}</td>
      <td class="num">${r.tx_relay ?? '--'}</td>
      <td class="num">${r.tx_relay_cancel ?? '--'}</td>
      <td class="num">${r.tx_dropped ?? '--'}</td>
      <td class="num" style="color:${noiseAccent(r.noise_floor)}">${r.noise_floor ?? '--'}</td>
      <td class="num">${r.heap_free != null ? fmtBytes(r.heap_free) : '--'}</td>
      <td class="num">${r.heap_total != null ? fmtBytes(r.heap_total) : '--'}</td>
      <td class="num">${r.num_online_nodes ?? '--'}</td>
      <td class="num">${r.num_total_nodes ?? '--'}</td>
      <td class="num" style="color:${
        r.tx_relay > 0 ? ((r.tx_relay/(r.tx_relay+(r.tx_relay_cancel||0))*100) >= 85 ? '#39ff14' : (r.tx_relay/(r.tx_relay+(r.tx_relay_cancel||0))*100) >= 65 ? '#ffcc00' : '#ff4444') : '#4a6580'
      }">${r.tx_relay > 0 ? (r.tx_relay/(r.tx_relay+(r.tx_relay_cancel||0))*100).toFixed(1)+'%' : '--'}</td>
      <td class="num" style="color:${
        r.rx_count > 0 ? (((r.rx_count-(r.rx_bad||0))/r.rx_count*100) >= 80 ? '#39ff14' : ((r.rx_count-(r.rx_bad||0))/r.rx_count*100) >= 60 ? '#ffcc00' : '#ff4444') : '#4a6580'
      }">${r.rx_count > 0 ? ((r.rx_count-(r.rx_bad||0))/r.rx_count*100).toFixed(1)+'%' : '--'}</td>
      <td class="num" style="color:${
        r.rx_count > 0 ? (((r.rx_dupe||0)/r.rx_count*100) <= 15 ? '#39ff14' : ((r.rx_dupe||0)/r.rx_count*100) <= 30 ? '#ffcc00' : '#ff4444') : '#4a6580'
      }">${r.rx_count > 0 ? ((r.rx_dupe||0)/r.rx_count*100).toFixed(1)+'%' : '--'}</td>
    </tr>
  `).join('');
}

function updateCharts(rows) {
  const labels = rows.map(r => r.ts ? r.ts.slice(11, 16) : '');
  const update = (chart, data) => {
    chart.data.labels   = labels;
    chart.data.datasets[0].data = data;
    chart.update();
  };
  update(charts.chutil,  rows.map(r => r.ch_util));
  update(charts.airutil, rows.map(r => r.air_util_tx));
  update(charts.battery, rows.map(r => r.battery));
  update(charts.noise,   rows.map(r => r.noise_floor));
  charts.packets.data.labels = labels;
  charts.packets.data.datasets[0].data = rows.map(r => r.tx_count);
  charts.packets.data.datasets[1].data = rows.map(r => r.rx_count);
  charts.packets.update();
}

// ── Status polling ──
function updateStatusBar(s) {
  const bar = document.getElementById('status-bar');
  bar.className = 'state-' + (s.state || 'idle');
  document.getElementById('status-msg').textContent = s.message || '';
  const busy = ['connecting','polling','disconnecting'].includes(s.state);
  document.getElementById('poll-btn').disabled = busy;
  document.getElementById('scan-btn').disabled = busy;
}

async function refreshStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    updateStatusBar(s);
  } catch(e) {}
}

async function refreshData() {
  try {
    const [latest, hist] = await Promise.all([
      fetch('/api/latest').then(r => r.json()),
      fetch('/api/history').then(r => r.json()),
    ]);
    _prev = hist.length > 1 ? hist[hist.length - 2] : null;
    renderCards(latest);
    updateCharts(hist);
    renderHistory(hist);
  } catch(e) { console.error('Data refresh error:', e); }
}

// ── Actions ──
async function doPoll() {
  document.getElementById('poll-btn').disabled = true;
  try {
    const res = await fetch('/api/poll', { method: 'POST' });
    const j   = await res.json();
    if (!j.ok) alert('Poll error: ' + j.error);
  } catch(e) { alert('Request failed: ' + e); }
}

async function doDisconnect() {
  document.getElementById('disc-btn').disabled = true;
  try {
    await fetch('/api/disconnect', { method: 'POST' });
    await refreshStatus();
  } catch(e) { alert('Disconnect failed: ' + e); }
  document.getElementById('disc-btn').disabled = false;
}

async function doScan() {
  document.getElementById('scan-btn').disabled = true;
  document.getElementById('scan-result').style.display = 'none';
  try {
    const res = await fetch('/api/scan', { method: 'POST' });
    const j   = await res.json();
    const el  = document.getElementById('scan-result');
    if (j.devices && j.devices.length > 0) {
      el.innerHTML = '🔍 Found: ' + j.devices.map(d =>
        `<b>${d.name}</b> @ <code>${d.address}</code>`
      ).join(' | ');
    } else {
      el.innerHTML = '⚠️ No Meshtastic BLE devices found. Is the radio on and within range?';
    }
    el.style.display = 'block';
  } catch(e) { alert('Scan failed: ' + e); }
  document.getElementById('scan-btn').disabled = false;
}

// ── Init ──
(async function init() {
  try { LAYOUT = (await fetch('/api/layout').then(r => r.json())).layout; } catch(e) {}
  renderCards({});        // empty placeholder cards in saved positions
  refreshData();
})();
refreshStatus();

// Adaptive polling: fast while busy, slow at idle
let _lastState = 'idle';
let _dataRefreshed = false;
async function adaptivePoll() {
  const s = await fetch('/api/status').then(r=>r.json()).catch(()=>({state:'idle'}));
  updateStatusBar(s);
  const busy = ['connecting','polling','disconnecting'].includes(s.state);
  // Refresh data once when transitioning from busy -> idle (poll just completed)
  if (!busy && _lastState !== 'idle' && _lastState !== 'error') {
    await refreshData();
    _dataRefreshed = true;
  }
  _lastState = s.state;
  // Schedule next poll: 1s while busy, 10s at idle
  setTimeout(adaptivePoll, busy ? 1000 : 10000);
}
adaptivePoll();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/status")
def api_status():
    with _status_lock:
        return jsonify(dict(_status))

@app.route("/api/poll", methods=["POST"])
def api_poll():
    with _status_lock:
        if _status["state"] not in ("idle", "error"):
            return jsonify({"ok": False, "error": "Poll already in progress"}), 409
    t = threading.Thread(target=_ble_poll_thread, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "BLE poll started"})

@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Scan for nearby Meshtastic BLE devices without connecting."""
    try:
        import meshtastic.ble_interface
        _install_long_scan()
        devices = meshtastic.ble_interface.BLEInterface.scan()
        result  = [{"name": d.name, "address": d.address} for d in devices]
        log.info(f"BLE scan found {len(result)} device(s): {result}")
        return jsonify({"ok": True, "devices": result})
    except ImportError as e:
        return jsonify({"ok": False, "error": str(e), "devices": []})
    except Exception as e:
        log.exception("BLE scan failed")
        return jsonify({"ok": False, "error": str(e), "devices": []})

@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    """Manually release the BLE connection so Android app can reconnect."""
    _release_iface()
    set_status("idle", "BLE disconnected — Android app can now reconnect.")
    return jsonify({"ok": True})

@app.route("/api/layout", methods=["GET"])
def api_layout_get():
    try:
        with open(LAYOUT_PATH, encoding="utf-8") as f:
            return jsonify({"layout": json.load(f)})
    except (FileNotFoundError, ValueError):
        return jsonify({"layout": None})

@app.route("/api/layout", methods=["POST"])
def api_layout_save():
    layout = (request.get_json(silent=True) or {}).get("layout")
    if layout is None:                      # reset to default order
        try: os.remove(LAYOUT_PATH)
        except FileNotFoundError: pass
        return jsonify({"ok": True})
    if (not isinstance(layout, list) or len(layout) > 120
            or not all(k is None or (isinstance(k, str) and len(k) < 64) for k in layout)):
        return jsonify({"ok": False, "error": "invalid layout"}), 400
    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(layout, f)
    return jsonify({"ok": True})

@app.route("/api/latest")
def api_latest():
    return jsonify(query_latest())

@app.route("/api/history")
def api_history():
    limit = request.args.get("limit", 300, type=int)
    return jsonify(query_history(limit))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    log.info(f"Starting dashboard at http://localhost:{SERVER_PORT}")
    log.info(">>> Make sure your phone's Bluetooth is OFF before clicking 'Poll Radio'")
    if BLE_DEVICE:
        log.info(f"BLE target: {BLE_DEVICE}")
    else:
        log.info("BLE target: auto-discover (first Meshtastic device found)")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, use_reloader=False)
