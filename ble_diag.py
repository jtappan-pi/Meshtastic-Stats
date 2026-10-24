"""
BLE visibility diagnostic for the Meshtastic radio.
Run with the phone's Bluetooth OFF:   python ble_diag.py
Listens to ALL BLE advertisements for 60s and reports every sighting
of the radio with signal strength (RSSI).

The radio's address comes from the same config.local.json the dashboard
uses (key "ble_device"); without one, matches by name ("mesh") only.
"""
import asyncio
import json
import os
import time

from bleak import BleakScanner

CONFIG_PATH = os.environ.get("MESH_DASHBOARD_CONFIG", "config.local.json")
DURATION = 60  # seconds


def _target_from_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            dev = json.load(f).get("ble_device")
    except (OSError, ValueError):
        return None
    return dev.upper() if isinstance(dev, str) else None


TARGET = _target_from_config()


async def main():
    sightings = []
    others = {}
    t0 = time.time()

    def cb(device, adv):
        addr = device.address.upper()
        name = adv.local_name or device.name or ""
        if (TARGET and addr == TARGET) or "mesh" in name.lower():
            sightings.append(adv.rssi)
            print(f"  +{time.time()-t0:5.1f}s  {addr}  rssi={adv.rssi:4d} dBm  name={name!r}")
        else:
            others[addr] = max(others.get(addr, -127), adv.rssi)

    print(f"Listening to ALL BLE traffic for {DURATION}s (target {TARGET or 'any name containing mesh'})...")
    print("Make sure the phone's Bluetooth is OFF.\n")
    async with BleakScanner(cb):
        await asyncio.sleep(DURATION)

    print("\n--- Summary ---")
    print(f"Other BLE devices heard: {len(others)}"
          f"  ({'adapter is receiving fine' if others else 'adapter heard NOTHING - BT stack/adapter problem'})")
    if sightings:
        print(f"Radio heard {len(sightings)} time(s). "
              f"RSSI min/avg/max: {min(sightings)}/{sum(sightings)//len(sightings)}/{max(sightings)} dBm")
        print("Rough guide: better than -80 = good | -80 to -90 = marginal | below -90 = too weak to connect reliably")
    else:
        print("Radio NEVER heard in 60s: either it is not advertising, or its signal is below this adapter's floor.")
        print("Cross-check: connect with the phone while standing next to this PC.")
        print("  Phone connects from here -> radio IS advertising -> this PC's BT reception is the bottleneck.")
        print("  Phone also fails from here -> the BLE path itself (radio placement) is the problem.")


if __name__ == "__main__":
    asyncio.run(main())
