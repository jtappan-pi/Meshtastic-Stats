"""Shared test setup.

Four jobs, all done before `server` is imported:

1. Guarantee that no test can touch a radio. The dev environment (WSL2) has no
   Bluetooth, and even on a Windows box tests must never grab the BLE link.
   Any attempt raises RadioAccessAttempted.
2. Stop server.py from opening mesh_dashboard.log in the project directory.
   It calls logging.basicConfig at import time, which is a no-op once the root
   logger already has a handler, so we give it one first.
3. Point server.py's config loader at a nonexistent file so tests always run on
   server.DEFAULTS, even when a real config.local.json sits in the project root.
4. Point server.py's DB and layout paths at a throwaway directory so that even a
   test that forgets the `db` fixture cannot touch the real mesh_stats.db.
"""

import logging
import os
import pathlib
import sys
import tempfile

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(PROJECT_ROOT))

logging.getLogger().addHandler(logging.NullHandler())


class RadioAccessAttempted(RuntimeError):
    """Raised when test code tries to open a BLE connection or scan."""


def _forbidden(*_args, **_kwargs):
    raise RadioAccessAttempted(
        "Tests must not connect to or scan for a radio. Use tests/fixtures/ data instead."
    )


import meshtastic.ble_interface as mbi  # noqa: E402

mbi.BLEInterface.__init__ = _forbidden
mbi.BLEInterface.scan = staticmethod(_forbidden)
mbi.BLEClient.__init__ = _forbidden

_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="meshstats-tests-"))
# Fresh mkdtemp directory, so this file cannot exist. Must stay above the
# `import server` line: the config is read at import time.
os.environ["MESH_DASHBOARD_CONFIG"] = str(_SCRATCH / "no-config.json")

import server  # noqa: E402

server.DB_PATH = str(_SCRATCH / "never-used.db")
server.LAYOUT_PATH = str(_SCRATCH / "never-used-layout.json")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point server at a fresh SQLite file and create the schema."""
    path = tmp_path / "telemetry.db"
    monkeypatch.setattr(server, "DB_PATH", str(path))
    server.init_db()
    return path


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    """Flask test client with isolated DB and layout file."""
    monkeypatch.setattr(server, "LAYOUT_PATH", str(tmp_path / "layout.json"))
    server.app.config["TESTING"] = True
    return server.app.test_client()
