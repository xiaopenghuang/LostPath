<div align="center">

<img src="ui/public/logo.png" alt="LostPath" width="120">

# LostPath

**Attribute Windows C: drive usage to the software that caused it, with reversible actions**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![Electron](https://img.shields.io/badge/Electron-33-47848F?logo=electron&logoColor=white)
![Tests](https://img.shields.io/badge/tests-325%20passing-brightgreen)

[中文](README.md) · **English**

</div>

---

Your applications live on D:, yet C: keeps filling up — VS Code caches into
`AppData\Roaming\Code`, Docker stores images under `ProgramData`, and `uv` and `pnpm` each
keep a cache somewhere else. LostPath scans C:, attributes every chunk of usage to the
software responsible, marks whether it is a regenerable cache or user data, and offers the
matching action: redirect via environment variable, clean up, or relocate to another drive
and leave a junction behind.

Every judgement is based on an actual scan of your machine and carries an evidence chain.
Scanning is read-only; before any write a rollback record is journaled, data goes to a
recycle area first, and everything can be undone for 30 days.

## Features

- **Attribution with evidence and confidence** — registry uninstall entries, Appx family
  names, shortcut targets, executable signatures and directory naming patterns are
  cross-referenced; every conclusion can be expanded to show its sources and weights.
- **Hard-link aware size accounting** — distinguishes `logical` (naive per-file sum),
  `dedup` (each inode once) and `freeable` (each inode once, all links inside the tree).
  A measured `uv` cache: 1.63 GB logical, 0.31 GB actually on disk.
- **Three actions, escalating by risk** — prefer the application's own documented
  environment variable (no files moved), then cleaning regenerable caches, and only then
  cross-drive relocation with a junction.
- **Undoable for 30 days** — a rollback manifest is written before execution, deleted data
  is moved to a recycle area rather than removed, and previous environment-variable values
  are journaled and restored on rollback.
- **Configurable target location** — defaults to the non-system drive with the most free
  space.
- **Explicit refusals** — directories currently in use, already reparse points, owned by
  the OS, below the confidence threshold, or targeting a full drive are never touched, and
  each refusal is listed with its reason.
- **Runs without elevation** — administrator rights are not required; blind spots caused by
  insufficient permissions are listed explicitly with paths and counts.
- **Light and dark themes** — text contrast measured against WCAG (≥4.5:1 body text, ≥3:1
  non-text elements); all interactive elements are keyboard accessible.

## Installation

Download `LostPath.Setup.<version>.exe` from
[Releases](https://github.com/xiaopenghuang/LostPath/releases) and run it. The target machine needs
no Python, conda or Node.

Requires Windows 10/11 x64. Core logic depends on `winreg`, PowerShell and NTFS
reparse-point semantics; there is no cross-platform plan.

## Running from source

```bash
# Backend
python -m pip install fastapi uvicorn

# Frontend
cd ui && npm install && npm run build && cd ..

# Start (the engine also serves the built frontend)
python engine/main.py
```

Open `http://127.0.0.1:8321`.

For the desktop shell (native window, single-instance lock, engine lifecycle):

```bash
cd desktop && npm install && npm start
```

The shell locates the engine in three steps: exe bundled into resources → repo
`dist/lostpath-engine.exe` → run from source via conda. The third requires conda on PATH,
or point at it explicitly:

```bash
set LOSTPATH_CONDA_EXE=D:\miniconda3\Scripts\conda.exe
set LOSTPATH_CONDA_ENV=lostpath
```

## Building

```bash
sh tools/build-release.sh
# With a specific interpreter:
LOSTPATH_PY=/d/conda/envs/lostpath/python.exe sh tools/build-release.sh
```

Steps: build frontend → icon → bundle engine with PyInstaller → produce installer with
electron-builder. **The order is load-bearing**: the engine exe embeds `ui/dist` and the
installer embeds the engine exe — both are snapshot copies, not references.

## Tech stack

| Layer | Technology |
|---|---|
| Attribution engine | Python 3.12 |
| Local service | FastAPI + uvicorn (`127.0.0.1:8321`, 19 JSON endpoints) |
| UI | React 18 + TypeScript 5 + Vite + Ant Design + AntV G6 |
| Desktop shell | Electron 33 |
| Packaging | PyInstaller + electron-builder (NSIS) |

## Project layout

```
lostpath/
  scan/         directory enumeration and evidence collection
  attribute/    attribution engine and knowledge base
  act/          planner (read-only) and executor
  storage/      snapshots and path resolution
engine/         FastAPI service and software ledger
ui/             frontend
desktop/        Electron shell
tests/          325 tests and redacted benchmark data
```

## Data location

Everything under `%LOCALAPPDATA%\LostPath\`:

```
snapshots/   scan snapshots     operations/  rollback journal
recycle/     recycle area (30d) icons/       icon cache
config/      user settings      logs/
```

Nothing is written to the install directory (no write access under `Program Files` without
elevation), and Roaming is not used — a snapshot describes *this* machine's disk, so
roaming it elsewhere would be wrong data. `LOSTPATH_DATA_DIR` relocates all of it.

## Tests

```bash
python -m pytest -q                  # fast suite, ~10s, reads redacted fixtures
python -m pytest -m integration -q   # integration, real engine + full scan, ~40s
```

`tools/install-hooks.sh` installs a pre-commit hook that runs the fast suite.

The attribution benchmark `tests/fixtures/machine-a/` is a redacted snapshot of a real
machine (username, SID and environment variables removed) and ships with the repo, so the
results are independently reproducible:

```bash
python tests/test_attribution_baseline.py
# 27 benchmark entries: correct 27, wrong 0, missing 0
# covering 106 traces / 50.65 GiB, of which 1.90 GiB unattributed
```

## Known limitations

- Bytes under long paths (>260 characters) are not yet counted.
- The `reclaimable` figure shown before execution is an upper bound; the exact `freeable`
  value is measured after execution and journaled.
- 65 Appx display names are unresolved; the UI shows the raw `ms-resource://` value.
- A few applications with self-integrity checks or anti-cheat do not tolerate junctions;
  roll back to restore.
- Running without elevation leaves blind spots (97 unreadable directories on the
  development machine, mostly other users' profiles and protected system directories).

## License

[MIT](LICENSE)
