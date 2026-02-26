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

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "21"))
TRACK_MAX_POINTS = int(os.getenv("TRACK_MAX_POINTS", "300"))

NORAD_IDS = []
raw_ids = os.getenv("NORAD_IDS", "")
for p in raw_ids.split(","):
    p = p.strip()
    if p.isdigit():
        NORAD_IDS.append(int(p))

# -----------------------------
# Dropbox
# -----------------------------

import dropbox
from dropbox.files import WriteMode

DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER", "/reentry_alerts")

def dropbox_client():
    return dropbox.Dropbox(
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
    )

# -----------------------------
# Models
# -----------------------------

@dataclass
class TipSolution:
    msg_epoch: str
    decay_epoch: str
    raw: dict

# -----------------------------
# Helpers
# -----------------------------

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def dt_to_iso_z(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def dt_to_iso_ph(t):
    return t.astimezone(PH_TZ).strftime("%Y-%m-%d %H:%M:%S (PH)")

def parse_utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

# -----------------------------
# Geometry
# -----------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def point_in_bbox(lat, lon):
    return (PH_BBOX["lat_min"] <= lat <= PH_BBOX["lat_max"] and
            PH_BBOX["lon_min"] <= lon <= PH_BBOX["lon_max"])

def distance_to_bbox_km(lat, lon):
    if point_in_bbox(lat, lon):
        return 0.0
    clamped_lat = min(max(lat, PH_BBOX["lat_min"]), PH_BBOX["lat_max"])
    clamped_lon = min(max(lon, PH_BBOX["lon_min"]), PH_BBOX["lon_max"])
    return haversine_km(lat, lon, clamped_lat, clamped_lon)

# -----------------------------
# Space Track
# -----------------------------

def spacetrack_login():
    s = requests.Session()
    r = s.post(LOGIN_URL, data={
        "identity": SPACE_TRACK_USERNAME,
        "password": SPACE_TRACK_PASSWORD
    })
    r.raise_for_status()
    return s

def fetch_tip(session, norad_id):
    url = f"https://www.space-track.org/basicspacedata/query/class/tip/NORAD_CAT_ID/{norad_id}/orderby/MSG_EPOCH desc/limit/{TIP_LIMIT}/format/json"
    return session.get(url).json()

def fetch_tle(session, norad_id):
    url = f"https://www.space-track.org/basicspacedata/query/class/gp/NORAD_CAT_ID/{norad_id}/orderby/EPOCH desc/limit/1/format/tle"
    txt = session.get(url).text.splitlines()
    return txt[-2], txt[-1]

# -----------------------------
# State
# -----------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_msg_epoch": {}}
    with open(STATE_PATH) as f:
        return json.load(f)

def save_state(state):
    ensure_dir(OUT_DIR)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

# -----------------------------
# Teams
# -----------------------------

def teams_send(event):
    if not TEAMS_WEBHOOK_URL:
        return
    title = f"[{event['severity']}] Reentry Update — {event['object_name']} (NORAD {event['norad_id']})"
    text = (
        f"Window: {event['window_start']} → {event['window_end']}\n"
        f"Hit: {event['hit_time_utc']} | {event['hit_time_ph']}\n"
        f"Distance: {event['distance_km']} km"
    )
    requests.post(TEAMS_WEBHOOK_URL, json={"text": f"**{title}**\n\n{text}"})

# -----------------------------
# Monitoring Logic
# -----------------------------

def check_one_object(session, norad_id, state):
    tips = fetch_tip(session, norad_id)
    if not tips:
        return None

    latest = tips[0]
    msg_epoch = latest.get("MSG_EPOCH")
    if state["last_msg_epoch"].get(str(norad_id)) == msg_epoch:
        return None

    state["last_msg_epoch"][str(norad_id)] = msg_epoch

    decay = parse_utc(latest.get("DECAY_EPOCH"))
    window_start = decay - dt.timedelta(minutes=120)
    window_end = decay + dt.timedelta(minutes=120)

    l1, l2 = fetch_tle(session, norad_id)
    ts = sf_load.timescale()
    sat = EarthSatellite(l1, l2, f"NORAD {norad_id}", ts)

    times = []
    cur = window_start
    while cur <= window_end:
        times.append(cur)
        cur += dt.timedelta(seconds=STEP_SECONDS)

    t_sf = ts.from_datetimes(times)
    sub = sat.at(t_sf).subpoint()

    min_dist = 1e9
    best_idx = 0

    for i, (lat, lon) in enumerate(zip(sub.latitude.degrees, sub.longitude.degrees)):
        d = distance_to_bbox_km(lat, lon)
        if d < min_dist:
            min_dist = d
            best_idx = i

    if min_dist == 0:
        severity = "MAJOR"
    elif min_dist <= PH_NEAR_KM:
        severity = "MAJOR"
    else:
        severity = "MINOR"

    event = {
        "norad_id": norad_id,
        "object_name": f"NORAD {norad_id}",
        "severity": severity,
        "window_start": dt_to_iso_z(window_start),
        "window_end": dt_to_iso_z(window_end),
        "hit_time_utc": dt_to_iso_z(times[best_idx]),
        "hit_time_ph": dt_to_iso_ph(times[best_idx]),
        "distance_km": round(min_dist, 2)
    }

    return event

# -----------------------------
# Commands
# -----------------------------

def cmd_monitor_once():
    state = load_state()
    session = spacetrack_login()

    for norad in NORAD_IDS:
        event = check_one_object(session, norad, state)
        if event:
            print("New TIP detected:", event["severity"])
            teams_send(event)

    save_state(state)

def cmd_monitor_loop(interval=3600):
    print("Starting hourly monitor loop...")
    while True:
        try:
            cmd_monitor_once()
        except Exception as e:
            print("Error:", e)
        print("Sleeping...")
        time.sleep(interval)

def cmd_dummy_alert():
    event = {
        "norad_id": 99999,
        "object_name": "DUMMY TEST",
        "severity": "MAJOR",
        "window_start": dt_to_iso_z(now_utc()),
        "window_end": dt_to_iso_z(now_utc()),
        "hit_time_utc": dt_to_iso_z(now_utc()),
        "hit_time_ph": dt_to_iso_ph(now_utc()),
        "distance_km": 0
    }
    teams_send(event)
    print("Dummy alert sent.")

# -----------------------------
# Main
# -----------------------------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("monitor-once")
    loop = sub.add_parser("monitor-loop")
    loop.add_argument("--interval", type=int, default=3600)
    sub.add_parser("dummy-alert")

    args = parser.parse_args()

    if args.cmd == "monitor-once":
        cmd_monitor_once()
    elif args.cmd == "monitor-loop":
        cmd_monitor_loop(args.interval)
    elif args.cmd == "dummy-alert":
        cmd_dummy_alert()

if __name__ == "__main__":
    main()