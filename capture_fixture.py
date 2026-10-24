"""
Capture one real poll from the radio into a JSON test fixture.

Windows only: needs the Bluetooth adapter. Refuses to run under WSL.
Run with the phone's Bluetooth OFF and the dashboard server NOT running
(only one BLE client can hold the radio).

    python capture_fixture.py                       # -> tests/fixtures/poll_<UTC timestamp>.json
    python capture_fixture.py --out my_fixture.json
    python capture_fixture.py --all-nodes --keep-positions --keep-mac

Reuses server.py's connection logic (long unfiltered scan, retry/cooldown), so
what it sees is exactly what the dashboard sees.
"""

import argparse
import base64
import datetime
import importlib.metadata
import json
import os
import pathlib
import sys
import threading
import time
import traceback

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_DIR = HERE / "tests" / "fixtures"


def _refuse_if_no_bluetooth_possible():
    """Exit when running inside WSL. Windows is always allowed: when the current
    directory is the \\\\wsl.localhost\\ share, a relative "/proc/version" resolves to
    the Linux file through the share, so the os.name check must come first."""
    if os.name == "nt":
        return
    in_wsl = bool(os.environ.get("WSL_DISTRO_NAME"))
    proc_version = pathlib.Path("/proc/version")
    if not in_wsl and proc_version.exists():
        in_wsl = "microsoft" in proc_version.read_text().lower()
    if in_wsl:
        sys.exit("This is WSL: there is no Bluetooth here. Run this script from Windows PowerShell.")


def _write_fixture(fixture, out_arg, n_packets):
    if out_arg:
        out = pathlib.Path(out_arg)
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = DEFAULT_DIR / f"poll_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nWrote {out}  ({n_packets} telemetry packet(s))")
    print("Review it before keeping it: it contains your node ID and names.")
    sys.stdout.flush()


def _release_with_timeout(server, seconds=10):
    """meshtastic's BLEInterface.close() can hang on Windows (the bleak disconnect never
    returns). Run it on a daemon thread and, if it does not finish in time, end the
    process anyway: the OS drops the BLE link when the process exits."""
    print("Releasing BLE connection...")
    sys.stdout.flush()
    t = threading.Thread(target=server._release_iface, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        print(f"BLE release did not finish in {seconds}s; exiting anyway (the link drops with the process).")
        sys.stdout.flush()
        os._exit(0)


def _drop_keys(args):
    """Keys removed from every dict in the fixture: 'raw' protobuf objects always,
    GPS 'position' blocks and the radio's 'macaddr' unless the flags say otherwise."""
    drop = {"raw"}
    if not args.keep_positions:
        drop.add("position")
    if not args.keep_mac:
        drop.add("macaddr")
    return drop


def _jsonable(obj, drop):
    """Make meshtastic's packet/node dicts JSON-safe: remove the keys in `drop`,
    base64-encode bytes, stringify anything else JSON cannot hold."""
    if isinstance(obj, dict):
        return {k: _jsonable(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, drop) for v in obj]
    if isinstance(obj, bytes):
        return {"__bytes_b64__": base64.b64encode(obj).decode("ascii")}
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def main():
    _refuse_if_no_bluetooth_possible()

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output file (default: tests/fixtures/poll_<UTC timestamp>.json)")
    ap.add_argument("--wait", type=int, default=20, help="seconds to wait for local_stats (default 20)")
    ap.add_argument("--all-nodes", action="store_true", help="include every node in the node DB, not just this radio")
    ap.add_argument("--keep-positions", action="store_true", help="keep GPS position blocks (stripped by default)")
    ap.add_argument("--keep-mac", action="store_true", help="keep the radio's Bluetooth MAC (stripped by default)")
    args = ap.parse_args()
    drop = _drop_keys(args)

    from pubsub import pub

    import server  # noqa: PLC0415  (imports meshtastic lazily; sets up logging)

    packets = []

    def on_receive(packet, interface):  # signature fixed by the pubsub topic
        packets.append(_jsonable(packet, drop))
        ls = packet.get("decoded", {}).get("telemetry", {}).get("localStats")
        if ls:
            print(f"  got localStats: {ls}")

    pub.subscribe(on_receive, "meshtastic.receive.telemetry")
    iface = None
    try:
        print(f"Connecting to {server.BLE_DEVICE or 'first Meshtastic device found'} "
              f"(up to {server.BLE_MAX_ATTEMPTS} attempts, ~{server.BLE_SCAN_SECONDS}s scan each)...")
        iface = server._get_or_connect_iface()
        print("Connected. Letting the node DB settle...")
        time.sleep(3)

        my_num = iface.myInfo.my_node_num if iface.myInfo else None
        my_id = None
        nodes = {}
        for nid, node in (iface.nodes or {}).items():
            if node.get("num") == my_num:
                my_id = nid
            if args.all_nodes or node.get("num") == my_num:
                nodes[nid] = _jsonable(node, drop)
        print(f"Local node: num={my_num} id={my_id}; capturing {len(nodes)} node record(s)")

        dest = my_id or "^all"
        iface.sendTelemetry(telemetryType="local_stats", destinationId=dest, wantResponse=True)
        print(f"Requested local_stats from {dest}; waiting up to {args.wait}s...")
        deadline = time.time() + args.wait
        while time.time() < deadline:
            if any(p.get("decoded", {}).get("telemetry", {}).get("localStats") for p in packets):
                break
            time.sleep(0.5)
        else:
            print("  no localStats packet within the wait; saving what we have")

        meta = getattr(iface, "metadata", None)
        fixture = {
            "schema": 1,
            "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "meshtastic_version": importlib.metadata.version("meshtastic"),
            "firmware_version": getattr(meta, "firmware_version", None) if meta is not None else None,
            "my_node_num": my_num,
            "my_node_id": my_id,
            "nodes": nodes,
            "telemetry_packets": packets,
        }
        # Write BEFORE releasing: closing the BLE link is the step most likely to hang.
        _write_fixture(fixture, args.out, len(packets))
    except Exception:
        traceback.print_exc()   # printed now, because a hung release may end the process below
        raise
    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive.telemetry")
        except Exception:
            pass
        if iface is not None:
            _release_with_timeout(server)


if __name__ == "__main__":
    main()
