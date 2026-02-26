#!/usr/bin/env python3
from __future__ import annotations

import os
import json
import time
import math
import argparse
import datetime as dt
from typing import Dict, List, Tuple

import requests
from dotenv import load_dotenv
from skyfield.api import EarthSatellite, load as sf_load

# ==============================
# LOAD ENV
# ==============================

load_dotenv()

SPACE_TRACK_USERNAME = os.getenv("SPACE_TRACK_USERNAME")
SPACE_TRACK_PASSWORD = os.getenv("SPACE_TRACK_PASSWORD")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL")
VIEWER_BASE_URL = os.getenv("VIEWER_BASE_URL")  # REQUIRED

DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER", "/ssa_alerts")

OUT_DIR = "./alerts"
STATE_FILE = os.path.join(OUT_DIR, "state.json")

LOGIN_URL = "https://www.space-track.org/ajaxauth/login"
EARTH_RADIUS_KM = 6371.0088
PH_TZ = dt.timezone(dt.timedelta(hours=8))

PH_BBOX = {"lon_min": 115, "lon_max": 130, "lat_min": 4, "lat_max": 22}
NEAR_KM = 500

GLOBAL_TIP_LIMIT = 200
WINDOW_MIN = 120
STEP_SECONDS = 30
MONITOR_INTERVAL = 3600

# ==============================
# UTILS
# ==============================

def ensure_dir():
    os.makedirs(OUT_DIR, exist_ok=True)

def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def iso_z(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def iso_ph(t):
    return t.astimezone(PH_TZ).strftime("%Y-%m-%d %H:%M:%S (PH)")

def haversine(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def distance_to_ph(lat, lon):
    if PH_BBOX["lat_min"] <= lat <= PH_BBOX["lat_max"] and \
       PH_BBOX["lon_min"] <= lon <= PH_BBOX["lon_max"]:
        return 0
    clat = min(max(lat, PH_BBOX["lat_min"]), PH_BBOX["lat_max"])
    clon = min(max(lon, PH_BBOX["lon_min"]), PH_BBOX["lon_max"])
    return haversine(lat, lon, clat, clon)

def severity(hit_type):
    return "MAJOR" if hit_type in ["CROSSES_PH", "NEAR_PH"] else "MINOR"

def viewer_link(event_id):
    if not VIEWER_BASE_URL:
        return ""
    return f"{VIEWER_BASE_URL.rstrip('/')}/?event_id={event_id}"

# ==============================
# DROPBOX
# ==============================

def dropbox_upload(local_path, filename):
    import dropbox
    from dropbox.files import WriteMode
    from requests.exceptions import ReadTimeout

    dbx = dropbox.Dropbox(
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        timeout=300  # increase timeout
    )

    for attempt in range(3):
        try:
            with open(local_path, "rb") as f:
                dbx.files_upload(
                    f.read(),
                    f"{DROPBOX_FOLDER}/{filename}",
                    mode=WriteMode.overwrite
                )
            return f"{DROPBOX_FOLDER}/{filename}"

        except ReadTimeout:
            print(f"Dropbox timeout. Retry {attempt+1}/3...")
            time.sleep(3)

        except Exception as e:
            print(f"Dropbox upload failed: {e}")
            return None

    print("Dropbox upload skipped after retries.")
    return None

# ==============================
# SPACE TRACK
# ==============================

def login():
    s = requests.Session()
    r = s.post(LOGIN_URL, data={
        "identity": SPACE_TRACK_USERNAME,
        "password": SPACE_TRACK_PASSWORD
    })
    r.raise_for_status()
    return s

def fetch_global_tip(session):
    url = (
        "https://www.space-track.org/basicspacedata/query/"
        "class/tip/"
        "orderby/MSG_EPOCH desc/"
        "limit/5/"
        "emptyresult/show/"
        "format/json"
    )

    r = session.get(url)
    r.raise_for_status()
    data = r.json()

    if not data:
        print("TIP query returned empty result.")
        return []

    return data

def fetch_tle(session, norad):
    url = (
        "https://www.space-track.org/basicspacedata/query/"
        f"class/gp/NORAD_CAT_ID/{norad}/orderby/EPOCH desc/limit/1/format/tle"
    )
    r = session.get(url)
    r.raise_for_status()
    lines = [l.strip() for l in r.text.splitlines() if l.strip()]
    return lines[-2], lines[-1]

# ==============================
# GROUND TRACK
# ==============================

def generate_track(sat, center_time):
    ts = sf_load.timescale()
    times = []
    start = center_time - dt.timedelta(minutes=WINDOW_MIN)

    for i in range(int((WINDOW_MIN*2*60)/STEP_SECONDS)):
        times.append(start + dt.timedelta(seconds=i*STEP_SECONDS))

    t_sf = ts.from_datetimes(times)
    sub = sat.at(t_sf).subpoint()

    track = []
    for lat, lon, t in zip(sub.latitude.degrees, sub.longitude.degrees, times):
        track.append({
            "time_utc": iso_z(t),
            "lat": float(lat),
            "lon": float(lon)
        })
    return track

# ==============================
# BUILD EVENT
# ==============================

def build_event(session, norad, msg_epoch, decay_epoch):
    l1, l2 = fetch_tle(session, norad)
    ts = sf_load.timescale()
    sat = EarthSatellite(l1, l2, f"NORAD {norad}", ts)

    center = dt.datetime.fromisoformat(decay_epoch.replace("Z", "+00:00"))

    if center.tzinfo is None:
        center = center.replace(tzinfo=dt.timezone.utc)
    else:
        center = center.astimezone(dt.timezone.utc)
    track = generate_track(sat, center)

    min_dist = 1e9
    hit_idx = 0

    for i, p in enumerate(track):
        d = distance_to_ph(p["lat"], p["lon"])
        if d < min_dist:
            min_dist = d
            hit_idx = i

    if min_dist == 0:
        hit_type = "CROSSES_PH"
    elif min_dist <= NEAR_KM:
        hit_type = "NEAR_PH"
    else:
        hit_type = "NOT_NEAR_PH"

    event_id = f"{norad}_{msg_epoch.replace(':','').replace('-','')}"

    event = {
        "event_id": event_id,
        "norad_id": norad,
        "severity": severity(hit_type),
        "decay_window": {
            "start_utc": decay_epoch,
            "end_utc": decay_epoch
        },
        "hit": {
            "type": hit_type,
            "time_utc": track[hit_idx]["time_utc"],
            "time_ph": iso_ph(center),
            "lat": track[hit_idx]["lat"],
            "lon": track[hit_idx]["lon"],
            "distance_to_ph_bbox_km": min_dist
        },
        "track": track,
        "viewer_url": viewer_link(event_id)
    }

    return event_id, event

# ==============================
# TEAMS
# ==============================

def send_teams(event):
    if not TEAMS_WEBHOOK_URL:
        return

    payload = event.copy()
    requests.post(TEAMS_WEBHOOK_URL, json=payload)

# ==============================
# DUMMY
# ==============================

def dummy_alert():
    ensure_dir()

    center = now_utc()
    track = []

    for i in range(200):
        lat = 5 + i*0.05
        lon = 116 + i*0.05
        t = center - dt.timedelta(minutes=100) + dt.timedelta(seconds=i*30)
        track.append({
            "time_utc": iso_z(t),
            "lat": lat,
            "lon": lon
        })

    event_id = "dummy_test"
    event = {
        "event_id": event_id,
        "norad_id": 99999,
        "severity": "MAJOR",
        "decay_window": {
            "start_utc": iso_z(center),
            "end_utc": iso_z(center)
        },
        "hit": {
            "type": "CROSSES_PH",
            "time_utc": track[100]["time_utc"],
            "time_ph": iso_ph(center),
            "lat": track[100]["lat"],
            "lon": track[100]["lon"],
            "distance_to_ph_bbox_km": 0
        },
        "track": track,
        "viewer_url": viewer_link(event_id)
    }

    path = os.path.join(OUT_DIR, f"{event_id}.json")
    with open(path, "w") as f:
        json.dump(event, f, indent=2)

    dropbox_upload(path, f"{event_id}.json")
    send_teams(event)

    print("Dummy alert created with FULL ground track.")

# ==============================
# MONITOR
# ==============================

def monitor_once():
    ensure_dir()
    session = login()
    tips = fetch_global_tip(session)

    if not tips:
        print("No TIP found.")
        return

    # Load state
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

    last_seen = state.get("last_msg_epoch", {})

    # Group TIP rows by NORAD
    by_norad = {}
    for row in tips:
        norad = int(row["NORAD_CAT_ID"])
        by_norad.setdefault(norad, []).append(row)

    processed_any = False

    for norad, rows in by_norad.items():
        # Sort by newest MSG_EPOCH
        rows.sort(key=lambda x: x["MSG_EPOCH"], reverse=True)
        latest = rows[0]

        msg_epoch = latest["MSG_EPOCH"]
        decay_epoch = latest["DECAY_EPOCH"]

        # Skip if already processed
        if last_seen.get(str(norad)) == msg_epoch:
            continue

        print(f"New decay detected → NORAD {norad}")

        event_id, event = build_event(session, norad, msg_epoch, decay_epoch)

        path = os.path.join(OUT_DIR, f"{event_id}.json")
        with open(path, "w") as f:
            json.dump(event, f, indent=2)

        try:
            dropbox_upload(path, f"{event_id}.json")
        except Exception as e:
            print("Dropbox error (continuing monitoring):", e)
        send_teams(event)

        last_seen[str(norad)] = msg_epoch
        processed_any = True

    if processed_any:
        state["last_msg_epoch"] = last_seen
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        print("State updated.")
    else:
        print("No new decays.")

# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["dummy-alert", "monitor-once", "monitor-loop"])
    args = parser.parse_args()

    if args.cmd == "dummy-alert":
        dummy_alert()
    elif args.cmd == "monitor-once":
        monitor_once()
    elif args.cmd == "monitor-loop":
        monitor_loop()