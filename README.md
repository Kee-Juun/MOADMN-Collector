## MOADMN Collector (RPA bot)

This folder contains a standalone MOADMN automation bot built from the architecture used in `WVPUC0 Project/`, adapted to the workflow described in `MOADMN.md`.

### What it does
- Creates a run folder under `Downloads/MOADMN Collections/` (configurable).
- Automates the MO Administrative Hearing Portal: logs in, searches Matters, filters documents for **Order** items in the selected date range.
- Performs duplicate checking against SF-View/LNDoc (configurable) and skips duplicates.
- Downloads, renames, and produces an Excel run report.

### Run (dev)
From repo root:

```bash
python "MOADMN Project/main.py"
```

### Configuration
Edit `MOADMN Project/config.json`:
- `download_path`: where run folders are created under your user home
- `sf_view_url`: SF-View/LNDoc folder (can be `file://...` or a local path)
- `headless`: run browser headless (not recommended for first runs)


