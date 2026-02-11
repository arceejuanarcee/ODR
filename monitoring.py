#!/usr/bin/env python3
"""
Reentry/TIP Monitoring Pipeline + Dummy Test + Flask Viewer (ALL-IN-ONE)

✅ What you get in this single script
1) monitor      : polls Space-Track TIP + latest TLE, checks if predicted corridor crosses/near PH, sends Teams alert
2) dummy-alert  : creates a dummy “reentry” event JSON + posts a Teams message with clickable Flask link
3) serve        : runs a Flask server that shows the event page at /event/<event_id>
4) dummy-page   : creates a dummy event only (no Teams), so you can test Flask locally

Why this is practical
- Your monitor creates events into OUT_DIR
- Flask serves those events read-only
- Teams alerts point to the Flask URL (clickable)

Requirements
  pip install requests skyfield python-dotenv numpy flask

Optional
  pip install simplekml  (for KML export)

.env / Environment variables
  SPACE_TRACK_USERNAME=...
  SPACE_TRACK_PASSWORD=...
  TEAMS_WEBHOOK_URL=...                 (optional)
  BASE_URL=https://your-domain          (for clickable links in Teams)
  NORAD_IDS=66877,12345                 (monitor targets)
  POLL_SECONDS=600
  PH_NEAR_KM=500
  WINDOW_BEFORE_MIN=120
  WINDOW_AFTER_MIN=120
  STEP_SECONDS=30
  FALLBACK_UNCERT_MIN=48
  OUT_DIR=./reentry_alerts
  FLASK_HOST=0.0.0.0
  FLASK_PORT=8080

Usage
  # 1) Run Flask viewer (local)
  python reentry_pipeline_full.py serve

  # 2) Create a dummy event page only (no Teams)
  python reentry_pipeline_full.py dummy-page

  # 3) Create dummy event + post Teams alert with clickable link
  python reentry_pipeline_full.py dummy-alert

  # 4) Run real monitor loop (polling)
  python reentry_pipeline_full.py monitor

Tip for Teams testing
- BASE_URL must be reachable by Teams users (not localhost).
- For quick test: run serve locally and use a tunnel (cloudflared/ngrok) to get a public https URL,
  then set BASE_URL to that URL.
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

import numpy as np
import requests
from dotenv import load_dotenv

from flask import Flask, abort, Response

from skyfield.api import EarthSatellite, load as sf_load

try:
    import simplekml
except Exception:
    simplekml = None


# -----------------------------
# Load env + constants
# -----------------------------
load_dotenv()

LOGIN_URL = "https://www.space-track.org/ajaxauth/login"

PH_TZ = dt.timezone(dt.timedelta(hours=8))
EARTH_RADIUS_KM = 6371.0088

# Operational PH bbox (quick filter)
PH_BBOX = {
    "lon_min": 115.0,
    "lon_max": 130.0,
    "lat_min": 4.0,
    "lat_max": 22.0,
}

OUT_DIR = os.getenv("OUT_DIR", "./reentry_alerts")
STATE_PATH = os.path.join(OUT_DIR, "state.json")

SPACE_TRACK_USERNAME = os.getenv("SPACE_TRACK_USERNAME", "").strip()
SPACE_TRACK_PASSWORD = os.getenv("SPACE_TRACK_PASSWORD", "").strip()

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip()  # used for links in Teams

DEFAULT_TIP_LIMIT = int(os.getenv("TIP_LIMIT", "200"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "600"))
PH_NEAR_KM = float(os.getenv("PH_NEAR_KM", "500"))

WINDOW_BEFORE_MIN = int(os.getenv("WINDOW_BEFORE_MIN", "120"))
WINDOW_AFTER_MIN = int(os.getenv("WINDOW_AFTER_MIN", "120"))
STEP_SECONDS = int(os.getenv("STEP_SECONDS", "30"))
FALLBACK_UNCERT_MIN = float(os.getenv("FALLBACK_UNCERT_MIN", "48"))

FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8080"))

NORAD_IDS: List[int] = []
raw_ids = os.getenv("NORAD_IDS", "").strip()
if raw_ids:
    for p in raw_ids.split(","):
        p = p.strip()
        if p.isdigit():
            NORAD_IDS.append(int(p))


# -----------------------------
# Models
# -----------------------------
@dataclass
class TipSolution:
    msg_epoch: str
    decay_epoch: str
    raw: dict


# -----------------------------
# Time + formatting helpers
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def dt_to_iso_z(t: dt.datetime) -> str:
    t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%SZ")


def dt_to_iso_ph(t: dt.datetime) -> str:
    t = t.astimezone(PH_TZ)
    return t.strftime("%Y-%m-%d %H:%M:%S (PH)")


def parse_any_datetime_utc(s: str) -> dt.datetime:
    if not s:
        raise ValueError("Empty datetime string")

    txt = s.strip()
    if txt.endswith("Z"):
        txt2 = txt[:-1] + "+00:00"
    else:
        txt2 = txt

    try:
        d = dt.datetime.fromisoformat(txt2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(txt, fmt).replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(txt, fmt).replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue

    raise ValueError(f"Unrecognized datetime format: {s!r}")


# -----------------------------
# Geometry helpers
# -----------------------------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(1e-16, 1 - a)))
    return EARTH_RADIUS_KM * c


def point_in_bbox(lat: float, lon: float, bbox: Dict[str, float]) -> bool:
    return (bbox["lat_min"] <= lat <= bbox["lat_max"]) and (bbox["lon_min"] <= lon <= bbox["lon_max"])


def distance_to_bbox_km(lat: float, lon: float, bbox: Dict[str, float]) -> float:
    if point_in_bbox(lat, lon, bbox):
        return 0.0
    clamped_lat = min(max(lat, bbox["lat_min"]), bbox["lat_max"])
    clamped_lon = min(max(lon, bbox["lon_min"]), bbox["lon_max"])
    return haversine_km(lat, lon, clamped_lat, clamped_lon)


# -----------------------------
# TIP uncertainty parsing (best-effort)
# -----------------------------
def parse_uncertainty_seconds(val: Any) -> Optional[float]:
    if val is None:
        return None

    if isinstance(val, (int, float)):
        x = float(val)
        if x <= 10:
            return x * 3600.0
        if x <= 600:
            return x * 60.0
        return x

    s = str(val).strip().lower()
    if not s:
        return None

    s = s.replace("~", "").replace("≈", "").replace("about", "").replace("+/-", "±").replace("±", "").strip()

    total = 0.0
    found = False

    m = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    if m:
        total += float(m.group(1)) * 3600.0
        found = True

    m = re.search(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)\b", s)
    if m:
        total += float(m.group(1)) * 60.0
        found = True

    m = re.search(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds)\b", s)
    if m:
        total += float(m.group(1))
        found = True

    if found and total > 0:
        return total

    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        x = float(m.group(1))
        if x <= 10:
            return x * 3600.0
        if x <= 600:
            return x * 60.0
        return x

    return None


def tip_batch_uncertainty_seconds(latest_batch: List[TipSolution]) -> Optional[float]:
    candidate_keys = [
        "EPOCH_UNCERTAINTY",
        "EPOCH_UNC",
        "DECAY_UNCERTAINTY",
        "DECAY_UNC",
        "DECAY_EPOCH_UNCERTAINTY",
        "DECAY_EPOCH_UNC",
        "WINDOW",
        "WINDOW_WIDTH",
        "WINDOW_MINUTES",
    ]
    for s in latest_batch:
        raw = s.raw or {}
        for k in candidate_keys:
            if k in raw and raw[k] not in (None, "", "N/A"):
                sec = parse_uncertainty_seconds(raw[k])
                if sec and sec > 0:
                    return sec
    return None


# -----------------------------
# Space-Track helpers
# -----------------------------
def retry_get(session: requests.Session, url: str, tries: int = 6, timeout: int = 30) -> requests.Response:
    last_exc = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep((2 ** i) + random.random())
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            time.sleep((2 ** i) + random.random())
    raise RuntimeError(f"GET failed after retries: {url}") from last_exc


def spacetrack_login(username: str, password: str) -> requests.Session:
    if not username or not password:
        raise RuntimeError("Missing SPACE_TRACK_USERNAME / SPACE_TRACK_PASSWORD.")
    s = requests.Session()
    r = s.post(LOGIN_URL, data={"identity": username, "password": password}, timeout=30)
    r.raise_for_status()
    return s


def fetch_tip(session: requests.Session, norad_id: int, limit: int = DEFAULT_TIP_LIMIT) -> list:
    url = (
        f"https://www.space-track.org/basicspacedata/query/class/tip/"
        f"NORAD_CAT_ID/{norad_id}/orderby/MSG_EPOCH%20desc/limit/{int(limit)}/format/json"
    )
    r = retry_get(session, url)
    txt = r.text.strip()
    return r.json() if txt.startswith("[") else json.loads(txt)


def fetch_latest_tle(session: requests.Session, norad_id: int) -> Tuple[str, str, str]:
    url = (
        f"https://www.space-track.org/basicspacedata/query/class/gp/"
        f"NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/1/format/tle"
    )
    r = retry_get(session, url)
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError("TLE fetch returned insufficient lines.")
    if lines[0].startswith("1 ") and lines[1].startswith("2 "):
        name = f"NORAD {norad_id}"
        l1, l2 = lines[0], lines[1]
    else:
        if len(lines) < 3:
            raise RuntimeError("TLE fetch returned insufficient lines (expected name + 2 lines).")
        name = lines[0]
        l1, l2 = lines[1], lines[2]
    return name, l1, l2


def parse_tip_solutions(tip_json: list) -> List[TipSolution]:
    sols: List[TipSolution] = []
    for row in tip_json:
        msg = (row.get("MSG_EPOCH") or row.get("MSG_EPOCH ", "") or "").strip()
        dec = (row.get("DECAY_EPOCH") or "").strip()
        sols.append(TipSolution(msg_epoch=msg, decay_epoch=dec, raw=row))

    def key(sol: TipSolution):
        try:
            return parse_any_datetime_utc(sol.msg_epoch).timestamp()
        except Exception:
            return 0.0

    sols.sort(key=key, reverse=True)
    return sols


def select_latest_tip_batch(solutions: List[TipSolution]) -> List[TipSolution]:
    if not solutions:
        return []
    newest_msg = solutions[0].msg_epoch
    if not newest_msg:
        return solutions[:1]
    return [s for s in solutions if s.msg_epoch == newest_msg]


def compute_tip_window_from_latest_batch(
    solutions_latest_batch: List[TipSolution],
    fallback_uncert_minutes: float
) -> Tuple[Optional[dt.datetime], Optional[dt.datetime], str]:
    decays: List[dt.datetime] = []
    for s in solutions_latest_batch:
        if s.decay_epoch:
            try:
                decays.append(parse_any_datetime_utc(s.decay_epoch))
            except Exception:
                pass

    if not decays:
        return None, None, "none"

    wmin = min(decays)
    wmax = max(decays)

    if (wmax - wmin).total_seconds() > 0:
        return wmin, wmax, "tip_spread"

    tip_unc_sec = tip_batch_uncertainty_seconds(solutions_latest_batch)
    if tip_unc_sec and tip_unc_sec > 0:
        half = dt.timedelta(seconds=float(tip_unc_sec))
        return wmin - half, wmax + half, "tip_uncertainty"

    half = dt.timedelta(minutes=float(fallback_uncert_minutes))
    return wmin - half, wmax + half, "fallback_uncertainty"


# -----------------------------
# Ground-track
# -----------------------------
def groundtrack_corridor(
    sat: EarthSatellite,
    t_center: dt.datetime,
    minutes_before: int,
    minutes_after: int,
    step_seconds: int
) -> Tuple[List[float], List[float], List[dt.datetime]]:
    ts = sf_load.timescale()
    start = t_center - dt.timedelta(minutes=minutes_before)
    end = t_center + dt.timedelta(minutes=minutes_after)

    times_dt: List[dt.datetime] = []
    cur = start
    while cur <= end:
        times_dt.append(cur)
        cur += dt.timedelta(seconds=step_seconds)

    t_sf = ts.from_datetimes(times_dt)
    geoc = sat.at(t_sf)
    sub = geoc.subpoint()

    lats = list(sub.latitude.degrees)
    lons_raw = list(sub.longitude.degrees)
    lons = [((x + 180) % 360) - 180 for x in lons_raw]
    return lats, lons, times_dt


def export_kml_corridor(path: str, name: str, lats: List[float], lons: List[float]) -> None:
    if simplekml is None:
        return
    kml = simplekml.Kml()
    ls = kml.newlinestring(name=name)
    ls.coords = list(zip(lons, lats))
    ls.altitudemode = simplekml.AltitudeMode.clamptoground
    ls.extrude = 0
    kml.save(path)


# -----------------------------
# State + output
# -----------------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"last_msg_epoch": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_msg_epoch": {}}


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def write_event(out_dir: str, event_id: str, event: Dict[str, Any]) -> str:
    ensure_dir(out_dir)
    path = os.path.join(out_dir, f"{event_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2)
    return path


# -----------------------------
# Teams alerting
# -----------------------------
def teams_post(webhook_url: str, title: str, text: str) -> None:
    if not webhook_url:
        return
    payload = {"text": f"**{title}**\n\n{text}"}
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()


def format_alert(event_id: str, event: Dict[str, Any]) -> Tuple[str, str]:
    norad = event.get("norad_id", "N/A")
    name = event.get("object_name", f"NORAD {norad}")
    hit = event.get("hit", {})
    w = event.get("decay_window", {})
    pf = event.get("ph_filter", {})

    link_line = ""
    if BASE_URL:
        link_line = f"\n🔗 View details:\n{BASE_URL.rstrip('/')}/event/{event_id}\n"

    title = f"Reentry/TIP Alert — {hit.get('type','')} — {name} (NORAD {norad})"
    text = (
        f"- TIP MSG_EPOCH: {event.get('tip_msg_epoch_used','')}\n"
        f"- Decay window (UTC): {w.get('start_utc','')} → {w.get('end_utc','')} (mode={w.get('mode','')})\n"
        f"- Hit time: {hit.get('time_utc','')} | {hit.get('time_ph','')}\n"
        f"- Hit location: lat={hit.get('lat',0):.3f}, lon={hit.get('lon',0):.3f}\n"
        f"- Distance to PH bbox: {hit.get('distance_to_ph_bbox_km',0):.0f} km (near threshold={pf.get('near_km_threshold',PH_NEAR_KM):.0f} km)\n"
        f"{link_line}"
    )
    if event.get("kml_path"):
        text += f"\n- KML: {event['kml_path']}\n"
    return title, text


# -----------------------------
# Monitoring logic (one object)
# -----------------------------
def check_one_object(session: requests.Session, norad_id: int, state: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    tip_raw = fetch_tip(session, norad_id)
    sols_all = parse_tip_solutions(tip_raw)
    latest_batch = select_latest_tip_batch(sols_all)
    if not latest_batch:
        return None

    latest_msg_epoch = latest_batch[0].msg_epoch or ""
    last_seen = (state.get("last_msg_epoch", {}) or {}).get(str(norad_id))

    # no change => no alert
    if last_seen and latest_msg_epoch and latest_msg_epoch == last_seen:
        return None

    wmin, wmax, mode = compute_tip_window_from_latest_batch(latest_batch, FALLBACK_UNCERT_MIN)

    # update seen (so we don't spam on bad windows)
    state.setdefault("last_msg_epoch", {})[str(norad_id)] = latest_msg_epoch

    if not (wmin and wmax):
        return None

    name, l1, l2 = fetch_latest_tle(session, norad_id)
    ts = sf_load.timescale()
    sat = EarthSatellite(l1, l2, name, ts)

    t_center = wmin + (wmax - wmin) / 2
    lats, lons, times_dt = groundtrack_corridor(sat, t_center, WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN, STEP_SECONDS)

    inside_hits: List[int] = []
    near_hits: List[int] = []

    min_dist = 1e18
    min_idx = None

    for i, (lat, lon) in enumerate(zip(lats, lons)):
        d = distance_to_bbox_km(float(lat), float(lon), PH_BBOX)
        if d < min_dist:
            min_dist = d
            min_idx = i
        if d == 0.0:
            inside_hits.append(i)
        elif d <= PH_NEAR_KM:
            near_hits.append(i)

    triggered = bool(inside_hits or near_hits)
    if not triggered:
        return None

    def pick_best(idxs: List[int]) -> int:
        best = idxs[0]
        best_d = 1e18
        for j in idxs:
            dj = distance_to_bbox_km(float(lats[j]), float(lons[j]), PH_BBOX)
            if dj < best_d:
                best_d = dj
                best = j
        return best

    if inside_hits:
        idx = pick_best(inside_hits)
        hit_type = "CROSSES_PH"
    else:
        idx = pick_best(near_hits)
        hit_type = "NEAR_PH"

    event_id = f"{norad_id}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    event: Dict[str, Any] = {
        "created_utc": dt_to_iso_z(now_utc()),
        "norad_id": norad_id,
        "object_name": name,
        "tip_msg_epoch_used": latest_msg_epoch,
        "decay_window": {
            "start_utc": dt_to_iso_z(wmin),
            "end_utc": dt_to_iso_z(wmax),
            "mode": mode,
        },
        "corridor": {
            "center_utc": dt_to_iso_z(t_center),
            "before_min": WINDOW_BEFORE_MIN,
            "after_min": WINDOW_AFTER_MIN,
            "step_seconds": STEP_SECONDS,
        },
        "ph_filter": {
            "bbox": PH_BBOX,
            "near_km_threshold": PH_NEAR_KM,
            "min_distance_km": float(min_dist),
            "min_distance_time_utc": dt_to_iso_z(times_dt[min_idx]) if min_idx is not None else None,
            "min_distance_time_ph": dt_to_iso_ph(times_dt[min_idx]) if min_idx is not None else None,
        },
        "hit": {
            "type": hit_type,
            "time_utc": dt_to_iso_z(times_dt[idx]),
            "time_ph": dt_to_iso_ph(times_dt[idx]),
            "lat": float(lats[idx]),
            "lon": float(lons[idx]),
            "distance_to_ph_bbox_km": float(distance_to_bbox_km(float(lats[idx]), float(lons[idx]), PH_BBOX)),
        },
    }

    if simplekml is not None:
        kml_path = os.path.join(OUT_DIR, f"{event_id}.kml")
        export_kml_corridor(kml_path, f"{name} corridor", lats, lons)
        event["kml_path"] = os.path.abspath(kml_path)

    return event_id, event


# -----------------------------
# Dummy event generators
# -----------------------------
def create_dummy_event(event_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    t = now_utc()
    if event_id is None:
        event_id = f"dummy_66877_{t.strftime('%Y%m%d_%H%M%S')}"

    event = {
        "created_utc": dt_to_iso_z(t),
        "norad_id": 66877,
        "object_name": "DUMMY OBJECT (TEST ONLY)",
        "tip_msg_epoch_used": dt_to_iso_z(t),
        "decay_window": {
            "start_utc": dt_to_iso_z(t - dt.timedelta(minutes=30)),
            "end_utc": dt_to_iso_z(t + dt.timedelta(minutes=30)),
            "mode": "dummy",
        },
        "ph_filter": {
            "bbox": PH_BBOX,
            "near_km_threshold": PH_NEAR_KM,
            "min_distance_km": 0.0,
            "min_distance_time_utc": dt_to_iso_z(t),
            "min_distance_time_ph": dt_to_iso_ph(t),
        },
        "hit": {
            "type": "CROSSES_PH",
            "time_utc": dt_to_iso_z(t),
            "time_ph": dt_to_iso_ph(t),
            "lat": 14.5995,   # Manila-ish
            "lon": 120.9842,
            "distance_to_ph_bbox_km": 0.0,
        },
    }
    return event_id, event


# -----------------------------
# Flask Viewer
# -----------------------------
def render_event_html(event_id: str, event: Dict[str, Any]) -> str:
    hit = event.get("hit", {})
    w = event.get("decay_window", {})
    obj = event.get("object_name", "")
    norad = event.get("norad_id", "")

    # Lightweight, Teams-friendly HTML with OG tags (preview-friendly if public HTTPS)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Reentry Alert — NORAD {norad}</title>
  <meta property="og:title" content="Reentry Alert — NORAD {norad}" />
  <meta property="og:description" content="{hit.get('type','')} | {obj}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .card {{ max-width: 820px; border: 1px solid #ddd; border-radius: 12px; padding: 18px; }}
    h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
    .muted {{ color: #666; }}
    pre {{ background: #f6f6f6; padding: 12px; border-radius: 8px; overflow-x: auto; }}
    .row {{ margin: 10px 0; }}
    .tag {{ display: inline-block; padding: 4px 10px; border-radius: 999px; background: #eee; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Reentry/TIP Event</h1>
    <div class="muted">Event ID: <b>{event_id}</b></div>

    <div class="row"><span class="tag">{hit.get('type','')}</span></div>

    <div class="row"><b>Object:</b> {obj} (NORAD {norad})</div>
    <div class="row"><b>TIP MSG_EPOCH:</b> {event.get('tip_msg_epoch_used','')}</div>

    <div class="row"><b>Decay window (UTC):</b> {w.get('start_utc','')} → {w.get('end_utc','')} <span class="muted">(mode={w.get('mode','')})</span></div>

    <div class="row"><b>Hit time:</b> {hit.get('time_utc','')} | {hit.get('time_ph','')}</div>
    <div class="row"><b>Hit location:</b> lat={hit.get('lat',0):.4f}, lon={hit.get('lon',0):.4f} <span class="muted">(dist={hit.get('distance_to_ph_bbox_km',0):.0f} km)</span></div>

    <div class="row"><b>Raw JSON:</b></div>
    <pre>{json.dumps(event, indent=2)}</pre>
  </div>
</body>
</html>"""


def make_flask_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(
            "<h3>Reentry Event Viewer</h3><p>Use /event/&lt;event_id&gt;</p>",
            mimetype="text/html"
        )

    @app.route("/event/<event_id>")
    def view_event(event_id: str):
        path = os.path.join(OUT_DIR, f"{event_id}.json")
        if not os.path.exists(path):
            abort(404)
        try:
            with open(path, "r", encoding="utf-8") as f:
                event = json.load(f)
        except Exception:
            abort(500)
        html = render_event_html(event_id, event)
        return Response(html, mimetype="text/html")

    @app.route("/event/<event_id>.json")
    def view_event_json(event_id: str):
        path = os.path.join(OUT_DIR, f"{event_id}.json")
        if not os.path.exists(path):
            abort(404)
        with open(path, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="application/json")

    return app


# -----------------------------
# Commands
# -----------------------------
def cmd_dummy_page() -> None:
    ensure_dir(OUT_DIR)
    event_id, event = create_dummy_event()
    path = write_event(OUT_DIR, event_id, event)
    print("Dummy event saved:", path)
    print(f"Open (local): http://{FLASK_HOST}:{FLASK_PORT}/event/{event_id}")


def cmd_dummy_alert() -> None:
    if not TEAMS_WEBHOOK_URL:
        raise SystemExit("Missing TEAMS_WEBHOOK_URL in env/.env")
    if not BASE_URL:
        raise SystemExit("Missing BASE_URL in env/.env (must be reachable by Teams users)")

    ensure_dir(OUT_DIR)
    event_id, event = create_dummy_event()
    path = write_event(OUT_DIR, event_id, event)

    title, text = format_alert(event_id, event)
    teams_post(TEAMS_WEBHOOK_URL, title + " (DUMMY TEST)", text)

    print("Dummy event saved:", path)
    print("Teams alert posted. Link:", f"{BASE_URL.rstrip('/')}/event/{event_id}")


def cmd_serve() -> None:
    ensure_dir(OUT_DIR)
    app = make_flask_app()
    print(f"Serving OUT_DIR={OUT_DIR}")
    print(f"Open: http://{FLASK_HOST}:{FLASK_PORT}/")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)


def cmd_monitor() -> None:
    if not NORAD_IDS:
        raise SystemExit("No NORAD_IDS set. Put NORAD_IDS=66877,12345 in your .env")
    if not SPACE_TRACK_USERNAME or not SPACE_TRACK_PASSWORD:
        raise SystemExit("Missing SPACE_TRACK_USERNAME / SPACE_TRACK_PASSWORD")

    ensure_dir(OUT_DIR)
    state = load_state()
    session = spacetrack_login(SPACE_TRACK_USERNAME, SPACE_TRACK_PASSWORD)

    print(f"[{dt_to_iso_z(now_utc())}] Monitor starting. NORAD_IDS={NORAD_IDS} poll={POLL_SECONDS}s near={PH_NEAR_KM}km")

    while True:
        loop_started = now_utc()
        try:
            for norad in NORAD_IDS:
                try:
                    res = check_one_object(session, norad, state)
                    if res:
                        event_id, event = res
                        path = write_event(OUT_DIR, event_id, event)

                        title, text = format_alert(event_id, event)
                        print("\n" + "=" * 90)
                        print(title)
                        print(text)
                        print("- Saved event:", path)

                        if TEAMS_WEBHOOK_URL:
                            teams_post(TEAMS_WEBHOOK_URL, title, text)
                            print("- Sent Teams webhook alert.")

                except Exception as e:
                    print(f"[WARN] NORAD {norad}: {e}")

            save_state(state)

        except Exception as e:
            print(f"[WARN] Loop error: {e}")
            # re-login once
            try:
                session = spacetrack_login(SPACE_TRACK_USERNAME, SPACE_TRACK_PASSWORD)
                print("[INFO] Re-logged in to Space-Track.")
            except Exception as e2:
                print(f"[ERROR] Re-login failed: {e2}")

        elapsed = (now_utc() - loop_started).total_seconds()
        sleep_s = max(1.0, float(POLL_SECONDS) - elapsed)
        time.sleep(sleep_s)


# -----------------------------
# Entry
# -----------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Reentry/TIP monitor + dummy test + Flask viewer (single script).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run Flask viewer at /event/<event_id>")
    sub.add_parser("dummy-page", help="Create dummy event JSON only (for Flask testing)")
    sub.add_parser("dummy-alert", help="Create dummy event JSON and post Teams alert with clickable link")
    sub.add_parser("monitor", help="Run polling monitor loop (Space-Track TIP + TLE)")

    args = parser.parse_args()

    if args.cmd == "serve":
        cmd_serve()
    elif args.cmd == "dummy-page":
        cmd_dummy_page()
    elif args.cmd == "dummy-alert":
        cmd_dummy_alert()
    elif args.cmd == "monitor":
        cmd_monitor()
    else:
        raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
