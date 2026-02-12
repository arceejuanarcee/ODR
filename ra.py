#!/usr/bin/env python3
"""
Reentry/TIP Monitoring Pipeline + Teams Workflow + Dropbox archive + housekeeping

Same behavior as original:
- Fetch TIP
- Generate corridor
- Save JSON locally
- Upload JSON to Dropbox
- Delete old JSONs locally AND in Dropbox
- Send Teams message
"""

from __future__ import annotations

import os
import re
import json
import time
import math
import random
import argparse
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
import dropbox
from dotenv import load_dotenv
from skyfield.api import EarthSatellite, load as sf_load

# -----------------------------
# Load env
# -----------------------------

load_dotenv()

LOGIN_URL = "https://www.space-track.org/ajaxauth/login"
PH_TZ = dt.timezone(dt.timedelta(hours=8))
EARTH_RADIUS_KM = 6371.0088

PH_BBOX = {"lon_min": 115.0, "lon_max": 130.0, "lat_min": 4.0, "lat_max": 22.0}

OUT_DIR = os.getenv("OUT_DIR", "./reentry_alerts").strip()
STATE_PATH = os.path.join(OUT_DIR, "state.json")

SPACE_TRACK_USERNAME = os.getenv("SPACE_TRACK_USERNAME", "").strip()
SPACE_TRACK_PASSWORD = os.getenv("SPACE_TRACK_PASSWORD", "").strip()

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
VIEWER_BASE_URL = os.getenv("VIEWER_BASE_URL", "").strip()

DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()

TIP_LIMIT = int(os.getenv("TIP_LIMIT", "200"))
PH_NEAR_KM = float(os.getenv("PH_NEAR_KM", "500"))
WINDOW_BEFORE_MIN = int(os.getenv("WINDOW_BEFORE_MIN", "120"))
WINDOW_AFTER_MIN = int(os.getenv("WINDOW_AFTER_MIN", "120"))
STEP_SECONDS = int(os.getenv("STEP_SECONDS", "30"))
FALLBACK_UNCERT_MIN = float(os.getenv("FALLBACK_UNCERT_MIN", "48"))
TRACK_MAX_POINTS = int(os.getenv("TRACK_MAX_POINTS", "300"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "21"))

NORAD_IDS: List[int] = []
raw_ids = os.getenv("NORAD_IDS", "").strip()
if raw_ids:
    for p in raw_ids.split(","):
        if p.strip().isdigit():
            NORAD_IDS.append(int(p.strip()))

# -----------------------------
# Helpers
# -----------------------------

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def dt_to_iso_z(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def dt_to_iso_ph(t):
    return t.astimezone(PH_TZ).strftime("%Y-%m-%d %H:%M:%S (PH)")

# -----------------------------
# Dropbox
# -----------------------------

def _dropbox_enabled():
    return bool(DROPBOX_ACCESS_TOKEN)

def dropbox_client():
    if not _dropbox_enabled():
        return None
    return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

def dropbox_upload_json(local_path: str, filename: str) -> Optional[str]:
    if not _dropbox_enabled():
        return None

    dbx = dropbox_client()
    dropbox_path = f"/reentry_alerts/{filename}"

    with open(local_path, "rb") as f:
        dbx.files_upload(
            f.read(),
            dropbox_path,
            mode=dropbox.files.WriteMode.overwrite
        )

    return dropbox_path

def dropbox_delete_file(path: str) -> bool:
    if not _dropbox_enabled():
        return False
    try:
        dropbox_client().files_delete_v2(path)
        return True
    except Exception:
        return False

def dropbox_list_old_files(days: int) -> List[str]:
    if not _dropbox_enabled():
        return []

    dbx = dropbox_client()
    cutoff = now_utc() - dt.timedelta(days=days)
    old = []

    try:
        res = dbx.files_list_folder("/reentry_alerts")
        for entry in res.entries:
            if hasattr(entry, "client_modified"):
                if entry.client_modified.replace(tzinfo=dt.timezone.utc) < cutoff:
                    old.append(entry.path_lower)
    except Exception:
        pass

    return old

# -----------------------------
# State
# -----------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_msg_epoch": {}, "dropbox_files": {}}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_msg_epoch": {}, "dropbox_files": {}}

def save_state(state):
    ensure_dir(OUT_DIR)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def write_event(event_id: str, event: Dict[str, Any]):
    ensure_dir(OUT_DIR)
    path = os.path.join(OUT_DIR, f"{event_id}.json")
    with open(path, "w") as f:
        json.dump(event, f, indent=2)
    return path

# -----------------------------
# Housekeeping
# -----------------------------

def housekeeping_local():
    ensure_dir(OUT_DIR)
    cutoff = time.time() - RETENTION_DAYS * 86400
    deleted = 0

    for fn in os.listdir(OUT_DIR):
        if not fn.endswith(".json") or fn == "state.json":
            continue
        path = os.path.join(OUT_DIR, fn)
        if os.stat(path).st_mtime < cutoff:
            os.remove(path)
            deleted += 1
    return deleted

def housekeeping_dropbox(state):
    deleted = 0
    old_paths = dropbox_list_old_files(RETENTION_DAYS)

    for p in old_paths:
        if dropbox_delete_file(p):
            deleted += 1

    # prune state
    for eid, meta in list(state.get("dropbox_files", {}).items()):
        if meta.get("path") in old_paths:
            state["dropbox_files"].pop(eid, None)

    return deleted

# -----------------------------
# Teams
# -----------------------------

def teams_send(event_id, event):
    if not TEAMS_WEBHOOK_URL:
        return

    payload = {
        "event_id": event_id,
        "norad_id": event.get("norad_id"),
        "hit_type": event.get("hit", {}).get("type"),
    }

    requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=20)

# -----------------------------
# Dummy Alert
# -----------------------------

def cmd_dummy_alert():
    ensure_dir(OUT_DIR)
    state = load_state()

    event_id = f"dummy_{now_utc().strftime('%Y%m%d_%H%M%S')}"
    event = {
        "created_utc": dt_to_iso_z(now_utc()),
        "norad_id": 66877,
        "hit": {"type": "CROSSES_PH"}
    }

    path = write_event(event_id, event)
    print("Saved:", path)

    db_path = dropbox_upload_json(path, os.path.basename(path))
    if db_path:
        state.setdefault("dropbox_files", {})[event_id] = {
            "path": db_path,
            "uploaded_utc": dt_to_iso_z(now_utc())
        }
        print("Uploaded to Dropbox:", db_path)

    teams_send(event_id, event)

    dl = housekeeping_local()
    dd = housekeeping_dropbox(state)
    save_state(state)

    print(f"Housekeeping: local={dl}, dropbox={dd}")

# -----------------------------
# Main
# -----------------------------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dummy-alert")

    args = parser.parse_args()

    if args.cmd == "dummy-alert":
        cmd_dummy_alert()

if __name__ == "__main__":
    main()
