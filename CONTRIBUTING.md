# Contributing to Biruni GIS

Thank you for helping. Biruni GIS is a Python shell (`app/plugins/biruni/`) on
top of an unmodified QGIS engine, so most contributions are plain Python + Qt.

## Ground rules

- **Drive QGIS, don't fork it.** New features use the QGIS API and Processing.
  The engine in `engine/` is never patched.
- **Every user-visible string is English in the code and goes through `tr()`**
  (or the `NAME`/`GROUP`/`HELP` attributes of a tool). Run
  `scripts/extract_strings.py app/plugins/biruni app/plugins/biruni/i18n/ar.json`
  and add the Arabic text for anything it reports as missing.
- **New tools are Processing algorithms** in `app/plugins/biruni/algorithms/`:
  subclass `BiruniAlgorithm`, give it an `ID`, and it is registered
  automatically.
- **Never change the user's data silently.** Tools write new layers; fixes run
  in an undoable edit session.
- **Icons** are Tabler outline icons referenced by name; `scripts/collect_icons.py`
  copies the ones the code uses.

## Running the tests

Each test is a standalone script run with the engine's Python:

```powershell
engine\bin\python-qgis.bat tests\test_algorithms.py
```

`tests/test_opendata.py` needs the internet; the others run offline. GUI-level
checks use the self-test driver (`BIRUNI_SELFTEST`, see `devtools.py`).

## Reporting problems

Use **View ▸ Help ▸ Report a Problem** inside Biruni GIS: it saves a ZIP with
the version, system details and recent messages (never your data) and opens a
pre-filled issue.
