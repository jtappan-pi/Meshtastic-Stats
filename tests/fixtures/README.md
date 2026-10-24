# Test fixtures (local only)

Real captures from `capture_fixture.py` land here as `*.json`. They contain node IDs,
node names and the device public key, so they are **gitignored and never committed** —
each machine keeps its own. `tests/test_fixtures.py` replays whatever is here and skips
cleanly when the directory is empty (e.g. on a fresh clone).

To create one, run on a machine with Bluetooth (phone app disconnected):

```
python capture_fixture.py
```
