"""Hardware-free tests for server.py: helpers, SQLite layer, Flask routes."""

import pytest
from conftest import RadioAccessAttempted

import server

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"

# Values seen in a real poll (from mesh_dashboard.log, 2026-10-19).
DEVICE_METRICS = {
    "batteryLevel": 101,
    "voltage": 4.191,
    "channelUtilization": 0.0,
    "airUtilTx": 0.0,
    "uptimeSeconds": 611611,
}
LOCAL_STATS = {
    "uptimeSeconds": 611611,
    "numPacketsTx": 839,
    "numPacketsRx": 2453,
    "numPacketsRxBad": 1674,
    "numOnlineNodes": 1,
    "numTotalNodes": 2,
    "numRxDupe": 8,
    "numTxRelay": 669,
    "numTxRelayCanceled": 3,
    "heapTotalBytes": 130260,
    "heapFreeBytes": 57812,
    "noiseFloor": -111,
}


class _Dev:
    def __init__(self, address):
        self.address = address


class _Adv:
    def __init__(self, uuids):
        self.service_uuids = uuids


def test_filter_scan_results_matches_by_mac_or_service_uuid():
    pairs = [
        (_Dev("AA:BB:CC:DD:EE:01"), _Adv([])),                      # our radio, UUID missing from advert
        (_Dev("AA:BB:CC:DD:EE:FF"), _Adv([SERVICE_UUID.upper()])),  # another meshtastic node
        (_Dev("11:22:33:44:55:66"), _Adv(["0000180f-0000-1000-8000-00805f9b34fb"])),  # unrelated
        (_Dev(None), _Adv(None)),                                   # malformed entry
    ]
    out = server._filter_scan_results(pairs, "aabbccddee01", SERVICE_UUID)
    assert [d.address for d in out] == ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:FF"]


def test_filter_scan_results_without_target_uses_uuid_only():
    pairs = [(_Dev("AA:BB:CC:DD:EE:01"), _Adv([])), (_Dev("AA:BB:CC:DD:EE:FF"), _Adv([SERVICE_UUID]))]
    out = server._filter_scan_results(pairs, "", SERVICE_UUID)
    assert [d.address for d in out] == ["AA:BB:CC:DD:EE:FF"]


def test_utc_now_iso_keeps_v1_format():
    ts = server.utc_now_iso()
    assert len(ts) == 20 and ts[10] == "T" and ts.endswith("Z")   # 2026-10-19T00:57:25Z
    assert ts[11:16].replace(":", "").isdigit()                    # JS slices HH:MM from here


def test_select_local_node_prefers_my_node():
    nodes = {
        "!aaaaaaaa": {"num": 1, "deviceMetrics": {"batteryLevel": 50}},
        "!deadbeef": {"num": 0xDEADBEEF, "deviceMetrics": DEVICE_METRICS},
    }
    assert server.select_local_node(0xDEADBEEF, nodes) == ("!deadbeef", DEVICE_METRICS)


def test_select_local_node_falls_back_to_first_node():
    nodes = {
        "!aaaaaaaa": {"num": 1, "deviceMetrics": {"batteryLevel": 50}},
        "!deadbeef": {"num": 0xDEADBEEF},                 # mine, but no deviceMetrics yet
    }
    assert server.select_local_node(0xDEADBEEF, nodes) == ("!aaaaaaaa", {"batteryLevel": 50})
    assert server.select_local_node(999, nodes) == ("!aaaaaaaa", {"batteryLevel": 50})
    assert server.select_local_node(None, nodes) == ("!aaaaaaaa", {"batteryLevel": 50})


def test_select_local_node_empty_db():
    assert server.select_local_node(0xDEADBEEF, {}) == ("local", {})
    assert server.select_local_node(0xDEADBEEF, None) == ("local", {})
    assert server.select_local_node(1, {"!x": {"num": 1}}) == ("!x", {})


def test_extract_local_stats():
    packet = {"decoded": {"telemetry": {"localStats": LOCAL_STATS, "raw": object()}}}
    assert server.extract_local_stats(packet) == LOCAL_STATS
    assert server.extract_local_stats({"decoded": {"telemetry": {"deviceMetrics": DEVICE_METRICS}}}) == {}
    assert server.extract_local_stats({"decoded": {}}) == {}
    assert server.extract_local_stats({}) == {}


def test_build_row_mapping():
    row = server.build_row(DEVICE_METRICS, LOCAL_STATS, "!deadbeef", ts="2026-10-19T00:57:25Z")
    assert tuple(row) == server.ROW_COLUMNS
    assert row == {
        "ts": "2026-10-19T00:57:25Z", "ch_util": 0.0, "air_util_tx": 0.0, "battery": 101,
        "voltage": 4.191, "uptime_sec": 611611, "tx_count": 839, "rx_count": 2453,
        "rx_bad": 1674, "rx_dupe": 8, "tx_relay": 669, "tx_relay_cancel": 3,
        "heap_total": 130260, "heap_free": 57812, "num_online_nodes": 1, "num_total_nodes": 2,
        "noise_floor": -111, "tx_dropped": 0, "node_id": "!deadbeef",
    }


def test_build_row_without_local_stats():
    row = server.build_row(DEVICE_METRICS, {}, ts="x")
    assert row["node_id"] == "local"
    counters = ("tx_count", "rx_count", "rx_bad", "rx_dupe", "tx_relay", "tx_relay_cancel", "tx_dropped")
    optional = ("heap_total", "heap_free", "num_online_nodes", "num_total_nodes", "noise_floor")
    assert {row[k] for k in counters} == {0}
    assert all(row[k] is None for k in optional)


def test_save_and_query_roundtrip(db):
    ts = server.save_telemetry(DEVICE_METRICS, LOCAL_STATS, "!deadbeef")
    row = server.query_latest()
    assert row["ts"] == ts
    assert row["node_id"] == "!deadbeef"
    assert row["battery"] == 101
    assert row["voltage"] == pytest.approx(4.191)
    assert row["uptime_sec"] == 611611
    assert row["tx_count"] == 839
    assert row["rx_count"] == 2453
    assert row["rx_bad"] == 1674
    assert row["rx_dupe"] == 8
    assert row["tx_relay"] == 669
    assert row["tx_relay_cancel"] == 3
    assert row["heap_total"] == 130260
    assert row["heap_free"] == 57812
    assert row["num_online_nodes"] == 1
    assert row["num_total_nodes"] == 2
    assert row["noise_floor"] == -111
    assert row["tx_dropped"] == 0


def test_missing_local_stats_saves_zero_counters_and_null_extras(db):
    server.save_telemetry(DEVICE_METRICS, {}, "local")
    row = server.query_latest()
    assert row["tx_count"] == 0
    assert row["rx_count"] == 0
    assert row["heap_free"] is None
    assert row["noise_floor"] is None


def test_history_is_chronological_and_limited(db):
    for i in range(5):
        server.save_telemetry({**DEVICE_METRICS, "batteryLevel": i}, LOCAL_STATS)
    assert [r["battery"] for r in server.query_history(limit=3)] == [2, 3, 4]
    assert server.query_history() == server.query_history(limit=300)


def test_empty_db_queries(db):
    assert server.query_latest() == {}
    assert server.query_history() == []


def test_read_routes(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/latest").get_json() == {}
    assert client.get("/api/history").get_json() == []
    status = client.get("/api/status").get_json()
    assert set(status) >= {"state", "message", "last_polled", "ble_address"}


def test_history_route_honours_limit(client):
    for i in range(4):
        server.save_telemetry({**DEVICE_METRICS, "batteryLevel": i}, LOCAL_STATS)
    rows = client.get("/api/history?limit=2").get_json()
    assert [r["battery"] for r in rows] == [2, 3]


def test_layout_roundtrip_and_validation(client):
    assert client.get("/api/layout").get_json() == {"layout": None}
    assert client.post("/api/layout", json={"layout": ["battery", None, "voltage"]}).status_code == 200
    assert client.get("/api/layout").get_json() == {"layout": ["battery", None, "voltage"]}
    assert client.post("/api/layout", json={"layout": "nope"}).status_code == 400
    assert client.post("/api/layout", json={"layout": [1, 2]}).status_code == 400
    assert client.post("/api/layout", json={"layout": None}).status_code == 200
    assert client.get("/api/layout").get_json() == {"layout": None}


def test_ble_guard_is_active():
    import meshtastic.ble_interface as mbi

    with pytest.raises(RadioAccessAttempted):
        mbi.BLEInterface("AA:BB:CC:DD:EE:01")
    with pytest.raises(RadioAccessAttempted):
        mbi.BLEInterface.scan()


def test_db_target_linux_is_plain_path():
    assert server._db_target("/tmp/x/telemetry.db", windows=False) == ("/tmp/x/telemetry.db", False)
    assert server._db_target("mesh_stats.db", windows=False) == ("mesh_stats.db", False)


def test_db_target_windows_uses_lock_free_vfs():
    # relative path (the default): URI relative to cwd, which is the pushd-mapped drive
    assert server._db_target("mesh_stats.db", windows=True) == ("file:mesh_stats.db?vfs=win32-none", True)
    # drive-letter absolute path
    assert server._db_target(r"Z:\home\u\proj\mesh_stats.db", windows=True) == (
        "file:/Z:/home/u/proj/mesh_stats.db?vfs=win32-none", True)
    # UNC path: SQLite URIs cannot carry a host, so plain open
    unc = r"\\wsl.localhost\Ubuntu\home\u\mesh_stats.db"
    assert server._db_target(unc, windows=True) == (unc, False)


def test_db_uri_form_is_accepted_by_sqlite(tmp_path, monkeypatch):
    """The URI shape we build for Windows must be one sqlite3 accepts; check with the
    unix equivalent of the lock-free VFS, which exists on every platform build."""
    import sqlite3
    target, uri = server._db_target(str(tmp_path / "u.db"), windows=False)
    conn = sqlite3.connect(f"file:{target}?vfs=unix-none", uri=True)
    conn.execute("create table t(x)")
    conn.close()


def test_capture_fixture_guard(monkeypatch):
    """Blocks WSL, never blocks Windows (even with cwd on the \\\\wsl.localhost\\ share)."""
    import os

    import capture_fixture

    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    with pytest.raises(SystemExit):
        capture_fixture._refuse_if_no_bluetooth_possible()
    monkeypatch.setattr(os, "name", "nt")
    capture_fixture._refuse_if_no_bluetooth_possible()   # must return quietly


def test_load_config_defaults_when_file_absent(tmp_path):
    cfg = server.load_config(str(tmp_path / "nope.json"))
    assert cfg == server.DEFAULTS
    assert cfg is not server.DEFAULTS          # a copy, not the shared dict


def test_load_config_merges_overrides(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"ble_device": "AA:BB:CC:DD:EE:01", "server_port": 8080}', encoding="utf-8")
    cfg = server.load_config(str(p))
    assert cfg["ble_device"] == "AA:BB:CC:DD:EE:01"
    assert cfg["server_port"] == 8080
    assert cfg["db_path"] == server.DEFAULTS["db_path"]


def test_load_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"ble_devise": "typo"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        server.load_config(str(p))


def test_load_config_rejects_malformed_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        server.load_config(str(p))
    p.write_text('["a", "list"]', encoding="utf-8")
    with pytest.raises(SystemExit):
        server.load_config(str(p))


def test_tests_run_on_defaults_not_local_config():
    """conftest points MESH_DASHBOARD_CONFIG at a nonexistent file before
    importing server, so a real config.local.json must not leak in here."""
    assert server.BLE_DEVICE is None
    assert server.SERVER_HOST == server.DEFAULTS["server_host"]
