# Contributing to pycentauri

Thanks for your interest. This is a community-driven library for the Elegoo
Centauri Carbon; contributions of every size are welcome.

## Dev setup

```sh
git clone https://github.com/bjan/pycentauri
cd pycentauri
python -m venv .venv
. .venv/bin/activate
pip install -e ".[mcp,dev]"
```

## Running checks

```sh
ruff check .
ruff format --check .
mypy src
pytest
```

## Integration tests against a real printer

Unit tests are fully offline. To exercise the client against a real Centauri
Carbon on your LAN:

```sh
PYCENTAURI_TEST_HOST=192.168.1.x pytest tests/integration
```

These are skipped by default in CI.

## Protocol references

The Centauri Carbon speaks Elegoo's SDCP v3 over a WebSocket on port 3030.
Two projects helped decode the wire protocol:

- [`ELEGOO-3D/elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) — the
  official C++ SDK; `src/lan/adapters/elegoo_fdm_cc/` is the authoritative
  reference for the original Centauri Carbon.
- [`CentauriLink/Centauri-Link`](https://github.com/CentauriLink/Centauri-Link)
  — a Kivy desktop/mobile GUI; its `main.py` documents the SDCP envelope and
  subscription dance in detail.

When adding a new command code, update `pycentauri/sdcp.py` `Cmd` with a
comment linking to the upstream reference.

## Pull requests

- Keep changes small and focused.
- Add unit tests for any new parsing or envelope logic.
- Run `ruff format` before submitting.
- Update `CHANGELOG.md` under `[Unreleased]`.
