# Meshtastic-Stats

@CLAUDE.local.md

Machine- and user-specific facts (paths, which machine has Bluetooth, interpreter quirks)
live in the gitignored `CLAUDE.local.md` — create it from `CLAUDE.local.md.example` on each
machine and read it first when present.

## What this project is

A single-file Flask dashboard (`server.py`) for a RAK4631 / WisMesh Pocket v2 Meshtastic
radio. On demand it connects to the radio over Bluetooth LE (the `meshtastic` Python library
on top of `bleak`), reads `deviceMetrics` for the local node out of the node DB, asks the
radio for one `local_stats` telemetry packet, stores one row per poll in SQLite
(`mesh_stats.db`), and serves cards, charts and a history table at http://127.0.0.1:5000.

Other files:

- `config.local.json`: per-machine settings — radio MAC/BLE name (`ble_device`), bind
  host/port, file paths. **Gitignored: it holds the radio's MAC. Never commit a real MAC
  or move it into code.** Copy `config.example.json` and edit. No file = defaults
  (auto-discover, 127.0.0.1:5000); see `DEFAULTS` in `server.py`.
- `config.example.json`: committed template for the above.
- `ble_diag.py`: standalone 60 s BLE advertisement listener that reports the radio's RSSI.
  Reads `ble_device` from the same config file. Safe to import (main guard); only run it
  on a machine with Bluetooth.
- `capture_fixture.py`: one-shot poll that writes the raw device data to a JSON fixture
  under `tests/fixtures/`. Needs Bluetooth; refuses to run under WSL.
- `card_layout.json`: saved drag-and-drop card order, written by the dashboard. Gitignored.
- `mesh_stats.db`: the user's real poll history. Never delete, truncate or rewrite it.
  Gitignored.
- `mesh_dashboard.log`: runtime log (path in config), appended to by `server.py` and
  `capture_fixture.py`. Gitignored.
- `start_dashboard.bat` / `start_dashboard.sh`: Windows / Linux launchers. Each creates its
  venv on first run (`.venv-win` / `.venv`), installs `requirements.txt`, then runs
  `server.py`. The `.bat` auto-detects a Python to build the venv with (`BASE_PYTHON`
  override, `py -3`, Anaconda/Miniconda, python.org per-user installs; never the bare
  `python` Microsoft Store stub). `.bat` files must keep CRLF line endings.
- `docs/QUBES.md`: running the dashboard — including BLE polling — in a Qubes OS app qube.

## Split workflow (read this first)

Development may happen on a machine without Bluetooth (for example WSL2), while the app
runs where the radio is reachable, possibly against the same folder through a share. The
project folder in use is the single source of truth; venvs are per-OS (`.venv` Linux,
`.venv-win` Windows) and git-ignored. Which machine can reach the radio, and how, is
recorded in `CLAUDE.local.md`.

## Hard rule: never attempt BLE from a machine without Bluetooth

WSL2 (and any adapterless environment) cannot reach the radio. **Never try to connect to,
scan for, or poll a live radio from such a machine.** Do not run `server.py`'s poll or scan
paths, `ble_diag.py`, `capture_fixture.py`, or `meshtastic --ble ...` there. They cannot
succeed and prove nothing.

Test parsing and stats logic against fixture data instead:

- Real captures live in `tests/fixtures/*.json` (produced by `capture_fixture.py` on a
  machine with Bluetooth). They are **local-only and gitignored**: captures contain node
  IDs, names and the device public key. Never commit one or weaken that ignore rule.
- `tests/conftest.py` replaces `BLEInterface.__init__`, `BLEInterface.scan` and
  `BLEClient.__init__` with functions that raise, so no test can open a BLE link even by
  accident. Keep that guard in place.
- Do not invent fixture files and present them as real captures. Synthetic sample data
  belongs inline in a test.

## Commands (Linux side, from the project root)

```bash
.venv/bin/python -m pytest                 # run the tests
.venv/bin/python -m ruff check .           # lint (rules in pyproject.toml)
.venv/bin/python -c "import server"        # import check; harmless, starts nothing
.venv/bin/python -m py_compile ble_diag.py capture_fixture.py   # syntax check
.venv/bin/python -m pip install -r requirements-dev.txt   # (re)install deps + pytest + ruff
```

`server.py` resolves the config file, DB, layout and log paths relative to the current
directory, so run everything from the project root. `tests/conftest.py` pre-empts the log
handler, points the config loader at a nonexistent file (tests always run on `DEFAULTS`,
never on a real `config.local.json`) and redirects the DB and layout paths to a temp
directory before importing `server`, so tests never touch real data.

Windows side (the user runs these, PowerShell, from the project folder — path in
`CLAUDE.local.md`):

```powershell
.\.venv-win\Scripts\python.exe server.py            # dashboard
.\.venv-win\Scripts\python.exe capture_fixture.py   # capture a new fixture
```

## Dependencies

`requirements.txt`: `meshtastic` (pinned `==2.7.11`, see Gotchas), `flask`, `pyserial`,
`bleak`, `pypubsub`. All of them install and import on Linux (bleak's Linux backend is
BlueZ over `dbus-fast`; it imports fine without an adapter). `requirements-dev.txt` adds
`pytest` and `ruff`. Python 3.10+ everywhere (Linux dev venv is 3.14).

## Tests

- `tests/test_smoke.py`: pure helpers (`_filter_scan_results`, `load_config`), SQLite
  save/query against a temp DB, Flask routes through the test client, and a check that the
  BLE guard is active.
- `tests/test_fixtures.py`: replays every `tests/fixtures/*.json` through the real parsing
  functions in `server.py` (`select_local_node`, `extract_local_stats`, `build_row`) and
  asserts the saved row matches. It is skipped when there are no fixtures (fresh clones:
  fixtures are local-only).
- Parsing lives in those three pure functions plus `utc_now_iso`; the poll thread only
  does transport. Keep new parsing logic there, not in the thread.
- Fixture format (`"schema": 1`): `captured_at`, `meshtastic_version`, `firmware_version`,
  `my_node_num`, `my_node_id`, `nodes` (node-DB dicts keyed by `!id`; `raw` protobufs and,
  by default, GPS `position` blocks and `macaddr` removed), `telemetry_packets` (decoded packet dicts received on
  `meshtastic.receive.telemetry`, bytes base64-encoded as `{"__bytes_b64__": ...}`).

## Gotchas

- `server.py` monkeypatches `meshtastic.ble_interface.BLEInterface.scan` with a long,
  unfiltered scan matched by MAC, because the radio's BLE signal can be marginal.
  That patch depends on library internals (`BLEClient.discover(return_adv=True)`,
  `SERVICE_UUID`). Verified against meshtastic 2.7.11 with bleak 3.0.2 on Windows and
  Linux (`BleakScanner.discover(return_adv=True)` is the stable cross-platform API);
  hence the `==2.7.11` pin. Re-check the patch after any meshtastic upgrade.
- The server deliberately keeps the BLE connection open between polls so the nRF52 does
  not stop advertising. Only one BLE client can hold the radio: the phone app, the
  dashboard and `capture_fixture.py` are mutually exclusive.
- The radio address lives in `config.local.json` (`ble_device`), loaded by
  `server.load_config()` at import time. Never hard-code it.
- On Linux/BlueZ the radio must be paired out-of-band first (`bluetoothctl`, PIN 123456);
  the meshtastic library never calls `pair()` itself. See `docs/QUBES.md`.
- **`BLEInterface.close()` can hang on Windows** (seen 2026-10-22: the meshtastic/bleak
  disconnect never returned). `capture_fixture.py` therefore writes its file before releasing
  and gives the release a 10 s timeout, then exits the process. The dashboard's DISCONNECT
  button calls the same `_release_iface()` without a timeout and may hang the poll thread;
  not fixed yet.
- **SQLite over the WSL share.** When Windows opens `mesh_stats.db` through
  `\\wsl.localhost\`, the 9P share rejects SQLite's byte-range locks and every statement
  fails with "database is locked". `server._db_target` therefore opens the DB with the
  lock-free `win32-none` VFS on Windows (plain open on Linux), and `server._db()` serializes
  all DB access inside the process with a lock. Never open the DB from a second process
  while the dashboard is running on Windows. Keep all DB access going through `_db()`.
- Git: history was re-created on 2026-10-24 after a privacy scrub (the old history
  contained the radio's MAC). Remote: a private GitHub repo as `origin`; pushing there is
  allowed. Never make the repo public, change its visibility, or add other remotes without
  the user's explicit say-so. Never commit `config.local.json`, `CLAUDE.local.md`,
  `tests/fixtures/*.json`, `mesh_stats.db`, `card_layout.json` or the log — and never
  commit a MAC address, node ID, or real capture data anywhere. Commit only when asked.
