import os
import json
import glob
import datetime as dt

import streamlit as st
import folium
from streamlit_folium import st_folium

# ---- Config
st.set_page_config(page_title="Reentry Event Viewer", layout="wide")

OUT_DIR = os.getenv("OUT_DIR", "./reentry_alerts")

PH_BBOX_DEFAULT = {"lon_min": 115.0, "lon_max": 130.0, "lat_min": 4.0, "lat_max": 22.0}


def load_event(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_events(out_dir: str):
    files = sorted(glob.glob(os.path.join(out_dir, "*.json")), reverse=True)
    # ignore state.json if present
    files = [p for p in files if os.path.basename(p).lower() != "state.json"]
    return files


def nice_ts(s: str) -> str:
    # best-effort for "YYYY-mm-dd HH:MM:SSZ"
    try:
        s = s.replace("Z", "").strip()
        t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return s


def _flatten_segments_to_poly(segments):
    """
    segments: list of segments
      each segment is a list of [lat, lon, time_str] OR [lat, lon]
    returns folium-ready list of (lat, lon)
    """
    poly = []
    for seg in segments or []:
        if not seg:
            continue
        for item in seg:
            if not item or len(item) < 2:
                continue
            lat = float(item[0])
            lon = float(item[1])
            poly.append((lat, lon))
    return poly


def _extract_poly_from_tracks_obj(tracks_obj):
    """
    tracks_obj from build_envelope_tracks(), e.g.:
      tracks_obj["mid"] -> list of segments -> each segment list of [lat, lon, time]
    Prefer mid track. Fallback to min/max.
    """
    if not isinstance(tracks_obj, dict):
        return []

    # Prefer mid, then min, then max
    for key in ("mid", "min", "max"):
        segs = tracks_obj.get(key)
        poly = _flatten_segments_to_poly(segs)
        if len(poly) >= 2:
            return poly

    # If still nothing, try first intermediate set
    inter = tracks_obj.get("intermediate") or []
    if inter and isinstance(inter, list):
        # inter is list of "segs"
        for segs in inter:
            poly = _flatten_segments_to_poly(segs)
            if len(poly) >= 2:
                return poly

    return []


st.title("Reentry Event Viewer (PH)")
st.caption(f"Reading events from: `{os.path.abspath(OUT_DIR)}`")

files = list_events(OUT_DIR)
if not files:
    st.warning("No event JSON files found. Run your monitor or dummy generator first.")
    st.stop()

# Build a selector label
labels = []
for p in files:
    try:
        e = load_event(p)
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

event = load_event(event_path)
event_id = os.path.splitext(os.path.basename(event_path))[0]

hit = event.get("hit", {})
decay = event.get("decay_window", {})
ph_bbox = event.get("ph_filter", {}).get("bbox", PH_BBOX_DEFAULT)

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
    st.write(f"Lat/Lon: **{float(hit.get('lat',0.0)):.4f}, {float(hit.get('lon',0.0)):.4f}**")
    st.write(f"Distance to PH bbox: **{float(hit.get('distance_to_ph_bbox_km',0.0)):.0f} km**")

st.divider()

# -----------------------------------------------------------------------------
# ✅ Ground track source selection:
# 1) Prefer legacy "track" (list of dict points)
# 2) Else use new "tracks" envelope (mid/min/max segments)
# -----------------------------------------------------------------------------
poly = []

track = event.get("track", []) or []
if track and isinstance(track, list) and isinstance(track[0], dict):
    # Old format
    poly = [(p["lat"], p["lon"]) for p in track if "lat" in p and "lon" in p]
else:
    # New format
    tracks_obj = event.get("tracks")
    poly = _extract_poly_from_tracks_obj(tracks_obj)

if not poly or len(poly) < 2:
    st.error(
        "No usable ground track found.\n\n"
        "Expected either:\n"
        "- `track`: list of {lat, lon, time_utc}\n"
        "- OR `tracks`: envelope object with segments like [lat, lon, time]\n"
    )
    st.stop()

# Center map
center = poly[len(poly) // 2]

m = folium.Map(location=center, zoom_start=5, control_scale=True)

# PH bbox rectangle
sw = (ph_bbox["lat_min"], ph_bbox["lon_min"])
ne = (ph_bbox["lat_max"], ph_bbox["lon_max"])
folium.Rectangle(bounds=[sw, ne], color="#ff7800", weight=2, fill=False).add_to(m)

# Track polyline
folium.PolyLine(poly, color="#1f77b4", weight=4, opacity=0.9).add_to(m)

# Hit marker
hit_lat = float(hit.get("lat", 0.0) or 0.0)
hit_lon = float(hit.get("lon", 0.0) or 0.0)
folium.CircleMarker(location=(hit_lat, hit_lon), radius=7, color="#d62728", fill=True, fill_opacity=0.85).add_to(m)

# Fit bounds
lats = [p[0] for p in poly]
lons = [p[1] for p in poly]
m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

st.subheader("Map + Ground Track")
st_folium(m, width=1200, height=540)

with st.expander("Raw JSON"):
    st.code(json.dumps(event, indent=2), language="json")
