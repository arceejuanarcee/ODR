import os
import json
import datetime as dt
from typing import Dict, List, Tuple, Optional

import streamlit as st
import folium
from streamlit_folium import st_folium

# Dropbox (optional)
try:
    import dropbox
    from dropbox.exceptions import ApiError, AuthError
except Exception:
    dropbox = None
    ApiError = None
    AuthError = None

# ---- Config
st.set_page_config(page_title="Reentry Event Viewer", layout="wide")

OUT_DIR = os.getenv("OUT_DIR", "./reentry_alerts").strip()

# Dropbox config
# OLD (short-lived): DROPBOX_ACCESS_TOKEN
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()

# NEW (long-term): refresh token flow
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "").strip()
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "").strip()
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "").strip()

DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER", "/reentry_alerts").strip()  # where your monitor uploads JSONs
DROPBOX_MAX_FILES = int(os.getenv("DROPBOX_MAX_FILES", "200"))  # limit listing for performance

PH_BBOX_DEFAULT = {"lon_min": 115.0, "lon_max": 130.0, "lat_min": 4.0, "lat_max": 22.0}


# Helpers

def nice_ts(s: str) -> str:
    try:
        s = s.replace("Z", "").strip()
        t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return s

def normalize_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0

def split_dateline_segments(points, jump_deg=180.0):
    if not points:
        return []
    segs = []
    cur = [points[0]]
    for i in range(1, len(points)):
        lat, lon = points[i]
        prev_lat, prev_lon = points[i - 1]
        if abs(lon - prev_lon) > jump_deg:
            if len(cur) >= 2:
                segs.append(cur)
            cur = [(lat, lon)]
        else:
            cur.append((lat, lon))
    if len(cur) >= 2:
        segs.append(cur)
    return segs

# -----------------------------
# Local file helpers (fallback)
# -----------------------------
def load_event_local(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def list_events_local(out_dir: str) -> List[str]:
    import glob
    files = sorted(glob.glob(os.path.join(out_dir, "*.json")), reverse=True)
    files = [p for p in files if os.path.basename(p).lower() != "state.json"]
    return files

# -----------------------------
# Dropbox helpers
# -----------------------------
@st.cache_resource
def get_dbx(access_token: str, app_key: str, app_secret: str, refresh_token: str):
    """
    Creates a Dropbox client.
    Priority:
      1) Refresh-token flow (recommended)
      2) Access-token flow (fallback)
    """
    if not dropbox:
        return None

    # Preferred: refresh token (long-term)
    if refresh_token and app_key and app_secret:
        return dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )

    # Fallback: short-lived access token
    if access_token:
        return dropbox.Dropbox(access_token)

    return None

def _is_dropbox_ready() -> Tuple[bool, str]:
    if not dropbox:
        return False, "Dropbox SDK not installed. Run: pip install dropbox"

    # Prefer refresh token config
    if DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET:
        return True, "OK (refresh token)"

    # Allow legacy access token as fallback
    if DROPBOX_ACCESS_TOKEN:
        return True, "OK (access token - may expire)"

    return False, "Missing Dropbox credentials. Set DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN (recommended)."

@st.cache_data(ttl=60, show_spinner=False)
def dropbox_list_json_files(
    access_token: str,
    app_key: str,
    app_secret: str,
    refresh_token: str,
    folder: str,
    max_files: int
) -> List[Dict[str, str]]:
    """
    Returns list of dicts:
      { "path": "/reentry_alerts/xxx.json", "name": "xxx.json", "server_modified": "...ISO...", "size": "..." }
    Only JSON files, excluding state.json.
    """
    dbx = get_dbx(access_token, app_key, app_secret, refresh_token)
    if dbx is None:
        return []

    # Normalize folder: must start with /
    if not folder.startswith("/"):
        folder = "/" + folder
    folder = folder.rstrip("/") or "/"

    out: List[Dict[str, str]] = []

    try:
        res = dbx.files_list_folder(folder, recursive=False)
        while True:
            for entry in res.entries:
                if isinstance(entry, dropbox.files.FileMetadata):
                    name = entry.name or ""
                    if not name.lower().endswith(".json"):
                        continue
                    if name.lower() == "state.json":
                        continue
                    out.append(
                        {
                            "path": entry.path_lower or entry.path_display or "",
                            "name": entry.name,
                            "server_modified": entry.server_modified.isoformat() if entry.server_modified else "",
                            "size": str(entry.size),
                        }
                    )
            if res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
            else:
                break
    except AuthError as e:
        raise RuntimeError(f"Dropbox AuthError: {e}")
    except ApiError as e:
        raise RuntimeError(f"Dropbox ApiError: {e}")

    out.sort(key=lambda x: x.get("server_modified", ""), reverse=True)
    if max_files > 0:
        out = out[: max_files]
    return out

@st.cache_data(ttl=60, show_spinner=False)
def dropbox_download_json(
    access_token: str,
    app_key: str,
    app_secret: str,
    refresh_token: str,
    path: str
) -> Dict:
    """
    Downloads JSON file from Dropbox and returns parsed dict.
    """
    dbx = get_dbx(access_token, app_key, app_secret, refresh_token)
    if dbx is None:
        raise RuntimeError("Dropbox client unavailable.")

    try:
        md, resp = dbx.files_download(path)
        data = resp.content.decode("utf-8")
        return json.loads(data)
    except AuthError as e:
        raise RuntimeError(f"Dropbox AuthError: {e}")
    except ApiError as e:
        raise RuntimeError(f"Dropbox ApiError: {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to download/parse JSON from Dropbox: {e}")

# -----------------------------
# UI
# -----------------------------
st.title("Reentry Event Viewer (PH)")

# Choose source
ready, why = _is_dropbox_ready()
use_dropbox_default = bool(ready)

source = st.radio(
    "Event source",
    ["Dropbox", "Local folder"],
    index=0 if use_dropbox_default else 1,
    horizontal=True,
)

if source == "Dropbox":
    if not ready:
        st.warning(
            f"Dropbox not ready: **{why}**\n\n"
            f"Recommended (no expiry): set **DROPBOX_APP_KEY**, **DROPBOX_APP_SECRET**, **DROPBOX_REFRESH_TOKEN**.\n"
            f"Fallback: set **DROPBOX_ACCESS_TOKEN** (will expire).\n"
            f"Also ensure scopes include **files.metadata.read** and **files.content.read**."
        )
        st.stop()

    mode_label = "refresh token" if (DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET) else "access token"
    st.caption(f"Reading events from Dropbox folder: `{DROPBOX_FOLDER}` (auth: {mode_label})")

    with st.expander("Dropbox connection", expanded=False):
        if st.button("Test Dropbox connection"):
            try:
                dbx = get_dbx(DROPBOX_ACCESS_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN)
                acct = dbx.users_get_current_account()
                st.success(f"OK: {acct.name.display_name}")
            except Exception as e:
                st.error(str(e))

    try:
        files_meta = dropbox_list_json_files(
            DROPBOX_ACCESS_TOKEN,
            DROPBOX_APP_KEY,
            DROPBOX_APP_SECRET,
            DROPBOX_REFRESH_TOKEN,
            DROPBOX_FOLDER,
            DROPBOX_MAX_FILES
        )
    except Exception as e:
        st.error(str(e))
        st.stop()

    if not files_meta:
        st.warning("No event JSON files found in Dropbox folder. Run your monitor first.")
        st.stop()

    labels: List[Tuple[str, str]] = []
    for fm in files_meta:
        path = fm["path"]
        name = fm["name"]
        eid = os.path.splitext(name)[0]
        mod = fm.get("server_modified", "")
        try:
            mod_label = dt.datetime.fromisoformat(mod.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S UTC") if mod else ""
        except Exception:
            mod_label = mod
        labels.append((f"{mod_label} | {eid}", path))

    label_to_path = {lab: path for lab, path in labels}
    selected_label = st.selectbox("Select an event", list(label_to_path.keys()), index=0)
    event_path = label_to_path[selected_label]

    try:
        event = dropbox_download_json(
            DROPBOX_ACCESS_TOKEN,
            DROPBOX_APP_KEY,
            DROPBOX_APP_SECRET,
            DROPBOX_REFRESH_TOKEN,
            event_path
        )
    except Exception as e:
        st.error(str(e))
        st.stop()

    event_id = os.path.splitext(os.path.basename(event_path))[0]

else:
    st.caption(f"Reading events from local folder: `{os.path.abspath(OUT_DIR)}`")

    files = list_events_local(OUT_DIR)
    if not files:
        st.warning("No event JSON files found locally. Run your monitor or dummy generator first.")
        st.stop()

    labels = []
    for p in files:
        try:
            e = load_event_local(p)
            eid = os.path.splitext(os.path.basename(p))[0]
            name = e.get("object_name", "")
            norad = e.get("norad_id", "")
            created = nice_ts(e.get("created_utc", ""))
            labels.append((f"{created} | {name} (NORAD {norad}) | {eid}", p))
        except Exception:
            labels.append((os.path.basename(p), p))

    label_to_path = {lab: path for lab, path in labels}
    selected_label = st.selectbox("Select an event", list(label_to_path.keys()), index=0)
    event_path = label_to_path[selected_label]

    event = load_event_local(event_path)
    event_id = os.path.splitext(os.path.basename(event_path))[0]

# -----------------------------
# Event summary
# -----------------------------
hit = event.get("hit", {}) or {}
decay = event.get("decay_window", {}) or {}
ph_bbox = (event.get("ph_filter", {}) or {}).get("bbox", PH_BBOX_DEFAULT)

col1, col2, col3 = st.columns([1.2, 1.2, 1.6])

with col1:
    st.subheader("Object")
    st.write(f"**{event.get('object_name','')}**")
    st.write(f"NORAD: **{event.get('norad_id','')}**")
    st.write(f"Event ID: `{event_id}`")

with col2:
    st.subheader("TIP / Window")
    st.write(f"TIP MSG_EPOCH: `{event.get('tip_msg_epoch_used','')}`")
    st.write(f"Window start: `{decay.get('start_utc','')}`")
    st.write(f"Window end: `{decay.get('end_utc','')}`")
    st.write(f"Mode: `{decay.get('mode','')}`")

with col3:
    st.subheader("Hit / Closest")
    st.write(f"Type: **{hit.get('type','')}**")
    st.write(f"Time UTC: `{hit.get('time_utc','')}`")
    st.write(f"Time PH: `{hit.get('time_ph','')}`")
    st.write(f"Lat/Lon: **{float(hit.get('lat',0.0) or 0.0):.4f}, {float(hit.get('lon',0.0) or 0.0):.4f}**")
    st.write(f"Distance to PH bbox: **{float(hit.get('distance_to_ph_bbox_km',0.0) or 0.0):.0f} km**")

st.divider()

# -----------------------------
# Ground track plotting
# -----------------------------
track = event.get("track", []) or []

poly = []
for p in track:
    if "lat" in p and "lon" in p:
        try:
            lat = float(p["lat"])
            lon = normalize_lon(float(p["lon"]))
            poly.append((lat, lon))
        except Exception:
            pass

if len(poly) < 2:
    st.error("No usable `track` points in this event JSON.")
    st.stop()

segments = split_dateline_segments(poly, jump_deg=180.0)
center = poly[len(poly) // 2]

m = folium.Map(location=center, zoom_start=5, control_scale=True)

# PH bbox rectangle
sw = (ph_bbox["lat_min"], ph_bbox["lon_min"])
ne = (ph_bbox["lat_max"], ph_bbox["lon_max"])
folium.Rectangle(bounds=[sw, ne], color="#ff7800", weight=2, fill=False).add_to(m)

# Draw segments
for seg in segments:
    folium.PolyLine(seg, color="#1f77b4", weight=4, opacity=0.9).add_to(m)

# Hit marker
hit_lat = float(hit.get("lat", 0.0) or 0.0)
hit_lon = normalize_lon(float(hit.get("lon", 0.0) or 0.0))
folium.CircleMarker(
    location=(hit_lat, hit_lon),
    radius=7,
    color="#d62728",
    fill=True,
    fill_opacity=0.85,
).add_to(m)

# Fit bounds
lats = [p[0] for p in poly]
lons = [p[1] for p in poly]
m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

st.subheader("Map + Ground Track")
st_folium(m, width=1200, height=540)

with st.expander("Raw JSON"):
    st.code(json.dumps(event, indent=2), language="json")
