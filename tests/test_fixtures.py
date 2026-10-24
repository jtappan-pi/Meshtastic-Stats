"""Replay every captured fixture in tests/fixtures/ through the real parsing path.

Fixtures are produced on Windows by capture_fixture.py. When there are none yet
the parametrized test is skipped.
"""

import json

import pytest
from conftest import FIXTURE_DIR

import server

FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def parse_fixture(fixture):
    """Run a fixture through the same functions the poll thread uses."""
    node_id, dm = server.select_local_node(fixture.get("my_node_num"), fixture["nodes"])
    ls = {}
    for packet in fixture.get("telemetry_packets", []):
        ls.update(server.extract_local_stats(packet))
    return dm, ls, node_id


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_replays_into_a_row(path, db):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture.get("schema") == 1, "unknown fixture schema"
    dm, ls, node_id = parse_fixture(fixture)
    assert dm, "fixture has no deviceMetrics for the local node"
    assert node_id == fixture.get("my_node_id"), "local node was not selected"

    server.save_telemetry(dm, ls, node_id)
    row = server.query_latest()

    assert row["node_id"] == node_id
    assert row["battery"] == dm.get("batteryLevel")
    assert row["voltage"] == pytest.approx(dm.get("voltage"))
    assert row["uptime_sec"] == dm.get("uptimeSeconds")
    if ls:
        assert row["tx_count"] == ls.get("numPacketsTx", 0)
        assert row["rx_count"] == ls.get("numPacketsRx", 0)
        assert row["rx_bad"] == ls.get("numPacketsRxBad", 0)
        assert row["tx_relay"] == ls.get("numTxRelay", 0)
        assert row["heap_free"] == ls.get("heapFreeBytes")
        assert row["noise_floor"] == ls.get("noiseFloor")
