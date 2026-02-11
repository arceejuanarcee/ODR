#!/usr/bin/env python3
"""
Reentry/TIP Monitoring — FIXED ground track + Teams webhook + Streamlit JSON

Commands:
  python monitoring.py dummy-page
  python monitoring.py dummy-alert
  python monitoring.py monitor-once --norad 56817
  python monitoring.py monitor-loop

Requirements:
  pip install requests skyfield python-dotenv numpy
"""

from __future__ import annotations
import os
import json
import time
import math
import argparse
import datetime as dt
from typing import Dict, List, Tuple, Optional

import requests
from dotenv import load_dotenv
from skyfield.api import EarthSatellite, load as sf_load

# ==============================
# ENV / CONFIG
# ==============================

load_dotenv()

LOGIN_URL = "https://www.space-track.org/ajaxauth/login"

SPACE_TRACK_USERNAME = os.getenv("SPACE_TRACK_USERNAME", "").strip()
SPACE_TRACK_PASSWORD = os.getenv("SPACE_TRACK_PASSWORD", "").strip()

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
VIEWER_BASE_URL = os.getenv("VIEWER_BASE_URL", "").strip().rstrip("/")

OUT_DIR = os.getenv("OUT_DIR", "./reentry_alerts")

TIP_LIMIT = int(os.getenv("TIP_LIMIT", "200"))
WINDOW_BEFORE_MIN = int(os.getenv("WINDOW_BEFORE_MIN", "120"))
WINDOW_AFTER_MIN = int(os.getenv("WINDOW_AFTER_MIN", "120"))
STEP_SECONDS = int(os.getenv("STEP_SECONDS", "30"))
PH_NEAR_KM = float(os.getenv("PH_NEAR_KM", "500"))

PH_TZ = dt.timezone(dt.timedelta(hours=8))
PH_BBOX = {"lon_min":115.0,"lon_max":130.0,"lat_min":4.0,"lat_max":22.0}

# ==============================
# HELPERS
# ==============================

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def dt_utc():
    return dt.datetime.now(dt.timezone.utc)

def iso_z(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def iso_ph(t):
    return t.astimezone(PH_TZ).strftime("%Y-%m-%d %H:%M:%S (PH)")

def parse_dt(s):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)

# ==============================
# GEOMETRY
# ==============================

EARTH_RADIUS_KM = 6371.0

def haversine(lat1,lon1,lat2,lon2):
    p1,p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2-lat1)
    dl = math.radians(lon2-lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*EARTH_RADIUS_KM*math.asin(math.sqrt(a))

def distance_to_bbox(lat,lon):
    if PH_BBOX["lat_min"] <= lat <= PH_BBOX["lat_max"] and \
       PH_BBOX["lon_min"] <= lon <= PH_BBOX["lon_max"]:
        return 0.0
    cl_lat = min(max(lat,PH_BBOX["lat_min"]),PH_BBOX["lat_max"])
    cl_lon = min(max(lon,PH_BBOX["lon_min"]),PH_BBOX["lon_max"])
    return haversine(lat,lon,cl_lat,cl_lon)

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

def fetch_tip(session,norad):
    url = f"https://www.space-track.org/basicspacedata/query/class/tip/NORAD_CAT_ID/{norad}/orderby/MSG_EPOCH desc/limit/{TIP_LIMIT}/format/json"
    r = session.get(url)
    r.raise_for_status()
    return r.json()

def fetch_tle(session,norad):
    url = f"https://www.space-track.org/basicspacedata/query/class/gp/NORAD_CAT_ID/{norad}/orderby/EPOCH desc/limit/1/format/tle"
    r = session.get(url)
    r.raise_for_status()
    lines=[l.strip() for l in r.text.splitlines() if l.strip()]
    if lines[0].startswith("1 "):
        return f"NORAD {norad}",lines[0],lines[1]
    return lines[0],lines[1],lines[2]

# ==============================
# GROUND TRACK (FIXED)
# ==============================

def groundtrack(sat,t_center):
    ts=sf_load.timescale()
    start=t_center-dt.timedelta(minutes=WINDOW_BEFORE_MIN)
    end=t_center+dt.timedelta(minutes=WINDOW_AFTER_MIN)

    times=[]
    cur=start
    while cur<=end:
        times.append(cur)
        cur+=dt.timedelta(seconds=STEP_SECONDS)

    t_sf=ts.from_datetimes(times)
    sub=sat.at(t_sf).subpoint()

    lats=list(sub.latitude.degrees)
    lons_raw=list(sub.longitude.degrees)

    # NORMALIZE LONGITUDE (-180 to 180)
    lons=[((x+180)%360)-180 for x in lons_raw]

    track=[]
    for lat,lon,t in zip(lats,lons,times):
        track.append({
            "time_utc": iso_z(t),
            "lat": float(lat),
            "lon": float(lon)
        })

    return track

# ==============================
# TEAMS
# ==============================

def send_teams(event_id,event):
    if not TEAMS_WEBHOOK_URL:
        print("No TEAMS_WEBHOOK_URL set.")
        return

    link = f"{VIEWER_BASE_URL}/?event_id={event_id}" if VIEWER_BASE_URL else ""

    payload={
        "event_id":event_id,
        "viewer_url":link,
        "norad_id":event["norad_id"],
        "object_name":event["object_name"],
        "window_start_utc":event["decay_window"]["start_utc"],
        "window_end_utc":event["decay_window"]["end_utc"],
        "hit_type":event["hit"]["type"],
        "hit_time_utc":event["hit"]["time_utc"],
        "hit_lat":event["hit"]["lat"],
        "hit_lon":event["hit"]["lon"]
    }

    r=requests.post(TEAMS_WEBHOOK_URL,json=payload)
    r.raise_for_status()
    print("Posted to Teams.")

# ==============================
# BUILD EVENT
# ==============================

def build_event(session,norad):

    tip=fetch_tip(session,norad)
    if not tip:
        raise RuntimeError("No TIP returned")

    msg_epoch=tip[0]["MSG_EPOCH"]
    batch=[r for r in tip if r["MSG_EPOCH"]==msg_epoch]

    decays=[parse_dt(r["DECAY_EPOCH"]) for r in batch if r.get("DECAY_EPOCH")]
    if not decays:
        raise RuntimeError("No DECAY_EPOCH")

    wmin=min(decays)
    wmax=max(decays)

    name,l1,l2=fetch_tle(session,norad)
    sat=EarthSatellite(l1,l2,name,sf_load.timescale())

    t_center=wmin+(wmax-wmin)/2
    track=groundtrack(sat,t_center)

    min_dist=1e9
    best=None
    for p in track:
        d=distance_to_bbox(p["lat"],p["lon"])
        if d<min_dist:
            min_dist=d
            best=p

    hit_type="CROSSES_PH" if min_dist==0 else ("NEAR_PH" if min_dist<=PH_NEAR_KM else "NOT_NEAR_PH")

    event_id=f"{norad}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    event={
        "created_utc":iso_z(dt_utc()),
        "norad_id":norad,
        "object_name":name,
        "tip_msg_epoch_used":msg_epoch,
        "decay_window":{
            "start_utc":iso_z(wmin),
            "end_utc":iso_z(wmax)
        },
        "hit":{
            "type":hit_type,
            "time_utc":best["time_utc"],
            "time_ph":iso_ph(parse_dt(best["time_utc"])),
            "lat":best["lat"],
            "lon":best["lon"],
            "distance_to_ph_bbox_km":float(min_dist)
        },
        "track":track
    }

    return event_id,event

# ==============================
# COMMANDS
# ==============================

def cmd_monitor_once(norad):
    ensure_dir(OUT_DIR)
    session=login()
    event_id,event=build_event(session,norad)

    path=os.path.join(OUT_DIR,f"{event_id}.json")
    with open(path,"w") as f:
        json.dump(event,f,indent=2)

    print("\nSaved:",path)
    print("Hit type:",event["hit"]["type"])
    print("Closest distance:",event["hit"]["distance_to_ph_bbox_km"],"km")

    if event["hit"]["type"]!="NOT_NEAR_PH":
        send_teams(event_id,event)

def cmd_dummy_page():
    ensure_dir(OUT_DIR)
    event_id="dummy_test"
    event={
        "created_utc":iso_z(dt_utc()),
        "norad_id":99999,
        "object_name":"DUMMY OBJECT",
        "tip_msg_epoch_used":"dummy",
        "decay_window":{
            "start_utc":iso_z(dt_utc()),
            "end_utc":iso_z(dt_utc())
        },
        "hit":{
            "type":"CROSSES_PH",
            "time_utc":iso_z(dt_utc()),
            "time_ph":iso_ph(dt_utc()),
            "lat":14.6,
            "lon":121.0,
            "distance_to_ph_bbox_km":0
        },
        "track":[
            {"time_utc":iso_z(dt_utc()),"lat":10,"lon":118},
            {"time_utc":iso_z(dt_utc()),"lat":14,"lon":121},
            {"time_utc":iso_z(dt_utc()),"lat":18,"lon":125}
        ]
    }
    path=os.path.join(OUT_DIR,f"{event_id}.json")
    with open(path,"w") as f:
        json.dump(event,f,indent=2)
    print("Dummy saved:",path)

# ==============================
# ENTRY
# ==============================

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)

    sub.add_parser("dummy-page")
    m=sub.add_parser("monitor-once")
    m.add_argument("--norad",type=int,required=True)

    args=parser.parse_args()

    if args.cmd=="dummy-page":
        cmd_dummy_page()
    elif args.cmd=="monitor-once":
        cmd_monitor_once(args.norad)

if __name__=="__main__":
    main()
