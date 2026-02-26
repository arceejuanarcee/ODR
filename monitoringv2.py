#!/usr/bin/env python3

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
from dotenv import load_dotenv
from skyfield.api import EarthSatellite, load as sf_load

# -----------------------------
# ENV + CONSTANTS
# -----------------------------

import sys
from pathlib import Path

def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

ENV_PATH = base_dir() / ".env"
load_dotenv(dotenv_path=ENV_PATH)
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
# MODELS
# -----------------------------
@dataclass
class TipSolution:
    msg_epoch: str
    decay_epoch: str
    raw: dict

# -----------------------------
# TIME HELPERS
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def dt_to_iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

# -----------------------------
# GEOMETRY
# -----------------------------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_KM * (2 * math.atan2(math.sqrt(a), math.sqrt(max(1e-16, 1 - a))))

def point_in_bbox(lat: float, lon: float) -> bool:
    return PH_BBOX["lat_min"] <= lat <= PH_BBOX["lat_max"] and \
           PH_BBOX["lon_min"] <= lon <= PH_BBOX["lon_max"]

# -----------------------------
# SPACE TRACK
# -----------------------------
def spacetrack_login(username: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(LOGIN_URL, data={"identity": username, "password": password}, timeout=30)
    r.raise_for_status()
    return s

def fetch_latest_tle(session: requests.Session, norad_id: int):
    url = (
        f"https://www.space-track.org/basicspacedata/query/class/gp/"
        f"NORAD_CAT_ID/{norad_id}/orderby/EPOCH desc/limit/1/format/tle"
    )
    r = session.get(url, timeout=30)
    r.raise_for_status()
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    return lines[-2], lines[-1]

# -----------------------------
# MONITOR LOGIC
# -----------------------------
def check_one_object(session: requests.Session, norad_id: int, verbose=True):

    l1, l2 = fetch_latest_tle(session, norad_id)
    ts = sf_load.timescale()
    sat = EarthSatellite(l1, l2, f"NORAD {norad_id}", ts)

    t_center = now_utc()
    times = [t_center + dt.timedelta(seconds=i*60) for i in range(-60, 60)]

    t_sf = ts.from_datetimes(times)
    geoc = sat.at(t_sf)
    sub = geoc.subpoint()

    triggered = False

    for lat, lon in zip(sub.latitude.degrees, sub.longitude.degrees):
        if point_in_bbox(lat, lon):
            triggered = True
            break

    if verbose:
        print(f"[NORAD {norad_id}] Triggered: {triggered}")

    return triggered

# -----------------------------
# COMMANDS
# -----------------------------
def cmd_monitor_once(verbose=True):

    if not NORAD_IDS:
        raise SystemExit("No NORAD_IDS set in .env")

    session = spacetrack_login(SPACE_TRACK_USERNAME, SPACE_TRACK_PASSWORD)

    print(f"\n[{dt_to_iso_z(now_utc())}] Running monitor-once")

    for norad in NORAD_IDS:
        try:
            check_one_object(session, norad, verbose=verbose)
        except Exception as e:
            print(f"[ERROR] NORAD {norad}: {e}")

def cmd_monitor_loop(interval_seconds=3600, verbose=True):

    print("="*60)
    print("HOURLY MONITOR LOOP STARTED")
    print(f"Interval: {interval_seconds} seconds")
    print("Press Ctrl+C to stop.")
    print("="*60)

    while True:
        start_time = now_utc()
        print(f"\n[{dt_to_iso_z(start_time)}] Starting cycle...")

        try:
            cmd_monitor_once(verbose=verbose)
        except Exception as e:
            print(f"[ERROR] Cycle failed: {e}")

        end_time = now_utc()
        duration = (end_time - start_time).total_seconds()

        print(f"[{dt_to_iso_z(end_time)}] Cycle finished in {duration:.1f}s")

        sleep_time = max(0, interval_seconds - duration)
        print(f"Sleeping {sleep_time:.1f} seconds...\n")

        try:
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            print("Monitor loop stopped by user.")
            break

# -----------------------------
# MAIN
# -----------------------------
def main():
    parser = argparse.ArgumentParser(description="Reentry Monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("monitor-once", help="Run monitor one time")

    pl = sub.add_parser("monitor-loop", help="Run monitor continuously")
    pl.add_argument("--interval", type=int, default=3600,
                    help="Interval in seconds (default=3600)")
    pl.add_argument("--quiet", action="store_true")

    args = parser.parse_args()

    if args.cmd == "monitor-once":
        cmd_monitor_once()
        return

    if args.cmd == "monitor-loop":
        cmd_monitor_loop(interval_seconds=args.interval,
                         verbose=(not args.quiet))
        return

if __name__ == "__main__":
    main()