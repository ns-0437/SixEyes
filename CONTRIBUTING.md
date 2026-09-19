# Contributing

## Setup

```bash
pip install -e ".[dev]"
```

## Checks CI runs

```bash
python -m pytest -q
python -m mypy
```

`mypy` runs in strict mode over `src/sixeyes` and `pilots`. To also run the offline pilot
against the pinned AgentFuse adapter, use `python -m pip install -r requirements-pilot.txt`
and then `python -m pilots.agentfuse --scenario restart --format json`.

## Pull requests

- Read [CLAUDE.md](CLAUDE.md) and [ARCHITECTURE.md](ARCHITECTURE.md) first; they record the
  read-only, no-content-retention constraints the code is built around.
- `pytest` must pass before a change is called done.
- Keep each PR to one change and link its issue with `Closes #N`.
