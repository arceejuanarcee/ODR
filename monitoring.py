#!/usr/bin/env python3
"""
Reentry/TIP Monitoring Pipeline + Teams Workflow + Google Drive archive + housekeeping

✅ Fetches latest TIP batch (orderby MSG_EPOCH desc)
✅ Generates a ground track corridor around window center
✅ Writes event JSON locally
✅ Uploads event JSON to Google Drive folder (service account)
✅ Deletes old JSONs (>RETENTION_DAYS) locally AND on Drive
✅ Sends Teams payload to Workflow/Power Automate trigger URL (recommended)

Env (.env):
  SPACE_TRACK_USERNAME=
  SPACE_TRACK_PASSWORD=
  NORAD_IDS=66877,56817
  TEAMS_WEBHOOK_URL=...        # Workflow trigger URL preferred
  VIEWER_BASE_URL=https://smcod-ssa.streamlit.app
  OUT_DIR=./reentry_alerts

Google Drive (recommended for Task Scheduler):
  GDRIVE_FOLDER_ID=...
  GOOGLE_SERVICE_ACCOUNT_FILE=C:\\path\\service_account.json

Housekeeping:
  RETENTION_DAYS=21

Optional tuning:
  TIP_LIMIT=200
  PH_NEAR_KM=500
  WINDOW_BEFORE_MIN=120
  WINDOW_AFTER_MIN=120
  STEP_SECONDS=30
  FALLBACK_UNCERT_MIN=48
  TRACK_MAX_POINTS=300
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
from dotenv import load_dotenv
from skyfield.api import EarthSatellite, load as sf_load

# Google Drive (service account)
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except Exception:
    service_account = None
    build = None
    MediaFileUpload = None

# -----------------------------
# Load env + constants
# -----------------------------

import sys
from pathlib import Path
from dotenv import load_dotenv

def base_dir() -> Path:
    # If packaged by PyInstaller
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

# Google Drive
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

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
# Time helpers
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


def downsample_track(track: List[Dict[str, Any]], max_points: int) -> List[Dict[str, Any]]:
    if max_points <= 0 or len(track) <= max_points:
        return track
    step = max(1, int(math.ceil(len(track) / float(max_points))))
    out = track[::step]
    if out and out[-1] is not track[-1]:
        out.append(track[-1])
    return out


# -----------------------------
# TIP uncertainty parsing
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
        "EPOCH_UNCERTAINTY", "EPOCH_UNC",
        "DECAY_UNCERTAINTY", "DECAY_UNC",
        "DECAY_EPOCH_UNCERTAINTY", "DECAY_EPOCH_UNC",
        "WINDOW", "WINDOW_WIDTH", "WINDOW_MINUTES",
    ]
    for sol in latest_batch:
        raw = sol.raw or {}
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


def fetch_tip(session: requests.Session, norad_id: int, limit: int = TIP_LIMIT) -> list:
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
        name, l1, l2 = lines[0], lines[1], lines[2]
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
# Ground track
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
    # normalize to [-180, 180)
    lons = [((x + 180.0) % 360.0) - 180.0 for x in lons_raw]
    return lats, lons, times_dt


# -----------------------------
# State + output
# -----------------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"last_msg_epoch": {}, "gdrive_files": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            stt = json.load(f)
            if "gdrive_files" not in stt:
                stt["gdrive_files"] = {}
            return stt
    except Exception:
        return {"last_msg_epoch": {}, "gdrive_files": {}}


def save_state(state: Dict[str, Any]) -> None:
    ensure_dir(OUT_DIR)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def write_event(out_dir: str, event_id: str, event: Dict[str, Any]) -> str:
    ensure_dir(out_dir)
    path = os.path.join(out_dir, f"{event_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2)
    return path


# -----------------------------
# Viewer link
# -----------------------------
def streamlit_event_link(event_id: str) -> str:
    if not VIEWER_BASE_URL:
        return ""
    base = VIEWER_BASE_URL.rstrip("/")
    return f"{base}/?event_id={event_id}"


# -----------------------------
# Teams posting
# -----------------------------
def _is_workflow_url(url: str) -> bool:
    u = (url or "").lower()
    return ("logic.azure.com" in u) or ("powerautomate" in u) or ("flow.microsoft" in u) or ("logic-" in u)


def teams_send(event_id: str, event: Dict[str, Any]) -> None:
    if not TEAMS_WEBHOOK_URL:
        return

    link = streamlit_event_link(event_id)
    payload = {
        "event_id": event_id,
        "viewer_url": link,
        "norad_id": event.get("norad_id"),
        "object_name": event.get("object_name"),
        "tip_msg_epoch_used": event.get("tip_msg_epoch_used"),
        "window_start_utc": event.get("decay_window", {}).get("start_utc"),
        "window_end_utc": event.get("decay_window", {}).get("end_utc"),
        "window_mode": event.get("decay_window", {}).get("mode"),
        "hit_type": event.get("hit", {}).get("type"),
        "hit_time_utc": event.get("hit", {}).get("time_utc"),
        "hit_time_ph": event.get("hit", {}).get("time_ph"),
        "hit_lat": event.get("hit", {}).get("lat"),
        "hit_lon": event.get("hit", {}).get("lon"),
        "distance_to_ph_bbox_km": event.get("hit", {}).get("distance_to_ph_bbox_km"),
        "near_threshold_km": event.get("ph_filter", {}).get("near_km_threshold"),
    }

    if _is_workflow_url(TEAMS_WEBHOOK_URL):
        r = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=25)
        r.raise_for_status()
        return

    # fallback (incoming webhook) - plain message only
    title = f"Reentry/TIP Alert — {payload.get('hit_type')} — {payload.get('object_name')} (NORAD {payload.get('norad_id')})"
    text = (
        f"- Window (UTC): {payload.get('window_start_utc')} → {payload.get('window_end_utc')} (mode={payload.get('window_mode')})\n"
        f"- Hit: {payload.get('hit_time_utc')} | {payload.get('hit_time_ph')}\n"
        f"- Location: lat={payload.get('hit_lat')}, lon={payload.get('hit_lon')}\n"
        f"- Dist to PH bbox: {payload.get('distance_to_ph_bbox_km')} km (threshold={payload.get('near_threshold_km')} km)\n"
    )
    if link:
        text += f"\nView details:\n{link}\n"

    r = requests.post(TEAMS_WEBHOOK_URL, json={"text": f"**{title}**\n\n{text}"}, timeout=25)
    r.raise_for_status()


# -----------------------------
# Google Drive helpers
# -----------------------------
def _drive_enabled() -> bool:
    return bool(GDRIVE_FOLDER_ID and GOOGLE_SERVICE_ACCOUNT_FILE and service_account and build and MediaFileUpload)


def drive_service():
    if not _drive_enabled():
        return None
    scopes = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive.metadata"]
    creds = service_account.Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_upload_json(local_path: str, filename: str) -> Optional[str]:
    """
    Uploads JSON file to Google Drive folder. Returns fileId.
    """
    if not _drive_enabled():
        return None

    svc = drive_service()
    if svc is None:
        return None

    media = MediaFileUpload(local_path, mimetype="application/json", resumable=False)
    body = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
    created = svc.files().create(body=body, media_body=media, fields="id").execute()
    return created.get("id")


def drive_delete_file(file_id: str) -> bool:
    if not _drive_enabled():
        return False
    svc = drive_service()
    if svc is None:
        return False
    try:
        svc.files().delete(fileId=file_id).execute()
        return True
    except Exception:
        return False


def drive_list_old_files_older_than(days: int) -> List[Dict[str, str]]:
    """
    Lists files in folder older than N days by createdTime.
    Returns list of {id, name, createdTime}.
    """
    if not _drive_enabled():
        return []
    svc = drive_service()
    if svc is None:
        return []

    cutoff = (now_utc() - dt.timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false and createdTime < '{cutoff}'"
    out: List[Dict[str, str]] = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=q,
            fields="nextPageToken, files(id,name,createdTime)",
            pageToken=page_token,
            pageSize=1000
        ).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


# -----------------------------
# Housekeeping
# -----------------------------
def housekeeping_local(out_dir: str, retention_days: int) -> int:
    """
    Deletes local event json files older than retention_days based on file mtime.
    Keeps state.json.
    Returns number deleted.
    """
    ensure_dir(out_dir)
    now = time.time()
    cutoff = now - float(retention_days) * 86400.0

    deleted = 0
    for fn in os.listdir(out_dir):
        if not fn.lower().endswith(".json"):
            continue
        if fn.lower() == "state.json":
            continue
        path = os.path.join(out_dir, fn)
        try:
            stt = os.stat(path)
            if stt.st_mtime < cutoff:
                os.remove(path)
                deleted += 1
        except Exception:
            pass
    return deleted


def housekeeping_drive(state: Dict[str, Any], retention_days: int) -> int:
    """
    Deletes Drive files older than retention_days by createdTime.
    Also prunes state['gdrive_files'] map if present.
    Returns number deleted.
    """
    if not _drive_enabled():
        return 0

    old_files = drive_list_old_files_older_than(retention_days)
    deleted = 0
    for f in old_files:
        fid = f.get("id")
        if fid and drive_delete_file(fid):
            deleted += 1

    # prune mapping
    gmap = state.get("gdrive_files", {}) or {}
    if gmap:
        # remove entries whose fileId no longer exists in a conservative way:
        # just drop ids we deleted above
        deleted_ids = {f.get("id") for f in old_files if f.get("id")}
        for eid, meta in list(gmap.items()):
            try:
                if isinstance(meta, dict) and meta.get("file_id") in deleted_ids:
                    gmap.pop(eid, None)
            except Exception:
                pass
        state["gdrive_files"] = gmap

    return deleted


# -----------------------------
# Monitoring logic (one object)
# -----------------------------
def check_one_object(session: requests.Session, norad_id: int, state: Dict[str, Any], verbose: bool = True) -> Optional[Tuple[str, Dict[str, Any]]]:
    tip_raw = fetch_tip(session, norad_id, limit=TIP_LIMIT)
    sols_all = parse_tip_solutions(tip_raw)
    latest_batch = select_latest_tip_batch(sols_all)
    if not latest_batch:
        if verbose:
            print(f"[NORAD {norad_id}] No TIP data returned.")
        return None

    latest_msg_epoch = latest_batch[0].msg_epoch or ""
    last_seen = (state.get("last_msg_epoch", {}) or {}).get(str(norad_id))

    if verbose:
        print(f"[NORAD {norad_id}] TIP latest MSG_EPOCH={latest_msg_epoch} (last_seen={last_seen})")

    # no change => no alert/event
    if last_seen and latest_msg_epoch and latest_msg_epoch == last_seen:
        if verbose:
            print(f"[NORAD {norad_id}] No new TIP batch. Skipping.")
        return None

    wmin, wmax, mode = compute_tip_window_from_latest_batch(latest_batch, FALLBACK_UNCERT_MIN)

    # update seen (avoid spam)
    state.setdefault("last_msg_epoch", {})[str(norad_id)] = latest_msg_epoch

    if not (wmin and wmax):
        if verbose:
            print(f"[NORAD {norad_id}] Could not compute decay window.")
        return None

    name, l1, l2 = fetch_latest_tle(session, norad_id)
    ts = sf_load.timescale()
    sat = EarthSatellite(l1, l2, name, ts)

    t_center = wmin + (wmax - wmin) / 2
    lats, lons, times_dt = groundtrack_corridor(sat, t_center, WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN, STEP_SECONDS)

    track = []
    for lat, lon, tt in zip(lats, lons, times_dt):
        track.append({"time_utc": dt_to_iso_z(tt), "lat": float(lat), "lon": float(lon)})
    track = downsample_track(track, TRACK_MAX_POINTS)

    min_dist = 1e18
    min_idx = None
    inside_hits: List[int] = []
    near_hits: List[int] = []

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

    # Always generate event JSON when TIP changed (so you can view track even if not near PH)
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
    elif near_hits:
        idx = pick_best(near_hits)
        hit_type = "NEAR_PH"
    else:
        # closest point overall
        idx = int(min_idx) if min_idx is not None else 0
        hit_type = "NOT_NEAR_PH"

    event_id = f"{norad_id}_{now_utc().strftime('%Y%m%d_%H%M%S')}"
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
            "track_points_saved": len(track),
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
        "track": track,
    }

    if verbose:
        print(f"[NORAD {norad_id}] Window: {event['decay_window']['start_utc']} -> {event['decay_window']['end_utc']} mode={mode}")
        print(f"[NORAD {norad_id}] Closest/Hit: {hit_type} dist={event['hit']['distance_to_ph_bbox_km']:.1f} km")
        print(f"[NORAD {norad_id}] Viewer: {streamlit_event_link(event_id) or '(missing VIEWER_BASE_URL)'}")

    return event_id, event


# -----------------------------
# Dummy event (for testing)
# -----------------------------
def create_dummy_event(event_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    t = now_utc()
    if event_id is None:
        event_id = f"dummy_66877_{t.strftime('%Y%m%d_%H%M%S')}"

    pts = [
        (10.0, 118.0),
        (12.0, 119.5),
        (14.6, 120.98),
        (16.0, 122.5),
        (18.0, 125.0),
    ]
    track = []
    for i in range(len(pts) - 1):
        lat1, lon1 = pts[i]
        lat2, lon2 = pts[i + 1]
        for k in range(18):
            f = k / 18.0
            lat = lat1 + (lat2 - lat1) * f
            lon = lon1 + (lon2 - lon1) * f
            tt = t - dt.timedelta(minutes=60) + dt.timedelta(minutes=6 * (i * 18 + k))
            track.append({"time_utc": dt_to_iso_z(tt), "lat": float(lat), "lon": float(lon)})
    track = downsample_track(track, TRACK_MAX_POINTS)

    hit_lat, hit_lon = 14.5995, 120.9842

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
            "lat": hit_lat,
            "lon": hit_lon,
            "distance_to_ph_bbox_km": 0.0,
        },
        "track": track,
    }
    return event_id, event


# -----------------------------
# Commands
# -----------------------------
def cmd_dummy_alert() -> None:
    ensure_dir(OUT_DIR)
    state = load_state()

    event_id, event = create_dummy_event()
    path = write_event(OUT_DIR, event_id, event)
    print("Dummy event saved:", path)
    print("Viewer link:", streamlit_event_link(event_id) or "(missing VIEWER_BASE_URL)")

    # Drive upload
    fid = drive_upload_json(path, os.path.basename(path))
    if fid:
        state.setdefault("gdrive_files", {})[event_id] = {"file_id": fid, "uploaded_utc": dt_to_iso_z(now_utc())}
        save_state(state)
        print("Uploaded to Google Drive fileId:", fid)
    else:
        print("Drive upload skipped (missing Drive config or libraries).")

    # Teams send
    if TEAMS_WEBHOOK_URL:
        teams_send(event_id, event)
        print("Posted to Teams.")
    else:
        print("TEAMS_WEBHOOK_URL missing -> no Teams message sent.")

    # housekeeping
    ensure_dir(OUT_DIR)
    dl = housekeeping_local(OUT_DIR, RETENTION_DAYS)
    dd = housekeeping_drive(state, RETENTION_DAYS)
    save_state(state)
    print(f"Housekeeping: local_deleted={dl}, drive_deleted={dd}")


def cmd_monitor_once(verbose: bool = True) -> None:
    if not NORAD_IDS:
        raise SystemExit("No NORAD_IDS set. Put NORAD_IDS=66877,56817 in your .env")
    if not SPACE_TRACK_USERNAME or not SPACE_TRACK_PASSWORD:
        raise SystemExit("Missing SPACE_TRACK_USERNAME / SPACE_TRACK_PASSWORD")

    ensure_dir(OUT_DIR)
    state = load_state()
    session = spacetrack_login(SPACE_TRACK_USERNAME, SPACE_TRACK_PASSWORD)

    print(f"[{dt_to_iso_z(now_utc())}] Monitor-once starting. NORAD_IDS={NORAD_IDS} TIP_LIMIT={TIP_LIMIT}")
    if TEAMS_WEBHOOK_URL:
        print("[INFO] Teams enabled. URL looks like Workflow?" , _is_workflow_url(TEAMS_WEBHOOK_URL))
    else:
        print("[INFO] Teams disabled (TEAMS_WEBHOOK_URL not set).")

    for norad in NORAD_IDS:
        try:
            res = check_one_object(session, norad, state, verbose=verbose)
            if not res:
                continue

            event_id, event = res

            # write local JSON
            path = write_event(OUT_DIR, event_id, event)
            print(f"[NORAD {norad}] Saved event JSON: {path}")

            # upload to Drive
            fid = drive_upload_json(path, os.path.basename(path))
            if fid:
                state.setdefault("gdrive_files", {})[event_id] = {"file_id": fid, "uploaded_utc": dt_to_iso_z(now_utc())}
                print(f"[NORAD {norad}] Uploaded to Drive fileId={fid}")
            else:
                print(f"[NORAD {norad}] Drive upload skipped (missing Drive config or libraries).")

            # send Teams ONLY if near/crosses PH
            hit_type = (event.get("hit", {}) or {}).get("type")
            if TEAMS_WEBHOOK_URL and hit_type in ("CROSSES_PH", "NEAR_PH"):
                teams_send(event_id, event)
                print(f"[NORAD {norad}] Teams sent (hit={hit_type}).")
            else:
                print(f"[NORAD {norad}] Teams NOT sent (hit={hit_type}).")

        except Exception as e:
            print(f"[WARN] NORAD {norad}: {e}")

    # housekeeping
    dl = housekeeping_local(OUT_DIR, RETENTION_DAYS)
    dd = housekeeping_drive(state, RETENTION_DAYS)
    save_state(state)
    print(f"Housekeeping done: local_deleted={dl}, drive_deleted={dd}")


def main() -> None:
    parser = argparse.ArgumentParser(description="TIP monitor + Teams + Drive archive + housekeeping.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dummy-alert", help="Create dummy JSON + upload to Drive + send Teams")
    pm = sub.add_parser("monitor-once", help="Check NORAD_IDS once (best for Task Scheduler)")
    pm.add_argument("--quiet", action="store_true", help="Less verbose output")

    args = parser.parse_args()

    if args.cmd == "dummy-alert":
        cmd_dummy_alert()
        return

    if args.cmd == "monitor-once":
        cmd_monitor_once(verbose=(not args.quiet))
        return

    raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
