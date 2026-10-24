# Running the dashboard on Qubes OS

Tested target: NitroPad V56 (Qubes OS 4.2+, Intel AX-210/211 or BE200 Wi-Fi/Bluetooth
combo card). The dashboard and BLE polling both run inside a single app qube.

**The one-radio rule still applies:** only one BLE client can hold the radio. The phone
app, a Windows dashboard, and this qube are mutually exclusive — disconnect the others
before polling from Qubes.

## 1. Template

A **Fedora** template is recommended (the Qubes certification notes for the V56 say the
firmware in Debian templates is currently too old for these Wi-Fi/BT cards). In the
template (not the app qube):

```bash
sudo dnf install bluez python3 git        # Fedora
# or, Debian template:
sudo apt install bluez python3-venv git
```

Shut the template down afterwards. Python packages do NOT go in the template — they live
in a venv in the app qube's home, which persists.

## 2. App qube

Create an app qube (say, `mesh`) based on that template, with networking enabled at least
for the initial `git clone` and `pip install`. You can detach networking afterwards if you
want the qube offline; BLE does not need it.

## 3. Attach the Bluetooth adapter

The combo card's Bluetooth function is an internal **USB** device, normally held by
`sys-usb`. In **dom0**:

```bash
qvm-usb                       # list; look for e.g. "sys-usb:2-3  Intel Corp. AX210 Bluetooth"
qvm-usb attach mesh sys-usb:2-3
```

The attachment does not survive a qube restart. Re-run it each boot, or make it
persistent with `qvm-usb attach --persistent mesh sys-usb:2-3`.

Then, inside the `mesh` qube:

```bash
sudo systemctl start bluetooth     # per-boot
```

To start bluetoothd automatically on qube boot, add `systemctl start bluetooth` to
`/rw/config/rc.local` in the qube.

## 4. Verify and pair (one-time)

Pairing must be done manually — the meshtastic Python library never pairs by itself, so
an unpaired radio fails with an authentication error on first connect.

```text
$ bluetoothctl
[bluetoothctl]# list            # a controller must be listed; if not, see Troubleshooting
[bluetoothctl]# scan on         # radio appears as Meshtastic_XXXX with its MAC
[bluetoothctl]# pair <MAC>      # PIN 123456 (Meshtastic default)
[bluetoothctl]# trust <MAC>
[bluetoothctl]# exit
```

Keep the phone app disconnected while you do this.

## 5. Install and run

```bash
git clone <your-private-remote-url> Meshtastic-Stats
cd Meshtastic-Stats
cp config.example.json config.local.json
# edit config.local.json: set "ble_device" to the radio's MAC
./start_dashboard.sh
```

Browse `http://127.0.0.1:5000` inside the qube.

To carry over your existing poll history, copy `mesh_stats.db` into the project folder
with `qvm-copy` / a USB stick — it is deliberately not in git.

## Troubleshooting

- **No controller in `bluetoothctl list`** — the USB attach didn't take, or firmware is
  missing. Check `qvm-usb` in dom0 (re-attach after every qube restart), then inside the
  qube `dmesg | grep -i bluetooth` for firmware load errors and `rfkill list` for a soft
  block (`rfkill unblock bluetooth`).
- **Connect fails with an authentication/encryption error** — the radio isn't paired in
  this qube. Do step 4; re-do it if the radio was factory-reset.
- **Radio found but connects are flaky** — marginal signal is already handled by the
  dashboard's long scan and retry logic; move the radio closer. USB passthrough
  (qvm-usb uses USBIP) can also drop under heavy traffic — detach and re-attach.
- **"database is locked"** — that is a Windows-over-WSL-share issue and cannot happen
  here; on Linux the DB is opened normally. If you see it, another process has the DB
  open — close it.
