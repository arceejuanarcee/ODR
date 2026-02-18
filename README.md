# SSA Reentry TIP Monitoring Pipeline

A production-ready Space Situational Awareness (SSA) pipeline that:

-   Fetches latest TIP (Tracking & Impact Prediction) data from
    Space-Track
-   Detects new reentry batches
-   Computes decay window and ground track corridor
-   Checks proximity to Philippine bounding box
-   Generates structured event JSON
-   Sends alert to Microsoft Teams (Workflow / Power Automate)
-   Uploads event JSON to Dropbox
-   Performs automatic housekeeping (deletes old files)
-   Can be packaged as a Windows .exe and scheduled hourly

------------------------------------------------------------------------

SYSTEM ARCHITECTURE

Space-Track API ↓ monitoring.py ↓ Ground Track + PH proximity logic ↓
Event JSON ↓ ┌───────────────┬───────────────┐ │ │ │ Teams Alert Dropbox
Upload Local Archive

Viewer:

Dropbox → Streamlit Viewer → Map + PH Bounding Box + Hit Marker

------------------------------------------------------------------------

FILE STRUCTURE

ODR/ │ ├── monitoring.py \# Main production pipeline ├──
reentry_viewer_app.py \# Streamlit viewer ├── requirements.txt ├── .env
\# Environment variables (local only) ├── reentry_alerts/ \# Local JSON
archive └── dist/ \# Compiled .exe output

------------------------------------------------------------------------

HOW THE MONITORING SCRIPT WORKS

1.  Fetch Latest TIP

The script queries Space-Track: - Pulls latest TIP batch - Orders by
MSG_EPOCH - Compares with stored last_seen - Skips if no new batch

2.  Compute Decay Window

Window Start = TIP_EPOCH - uncertainty\
Window End = TIP_EPOCH + uncertainty

Mode: - tip_uncertainty - or fallback to default window

3.  Ground Track Propagation

-   Propagates TLE over window
-   Generates lat/lon points
-   Normalizes longitudes
-   Splits segments across dateline

4.  PH Proximity Check

Bounding box: Lat: 4° to 22°\
Lon: 115° to 130°

Outputs: - HIT - NEAR_PH - NOT_NEAR_PH - Distance to PH bbox (km)

5.  Event JSON Generation

Saved as: reentry_alerts/{NORAD}\_{timestamp}.json

Contains: - Object name - NORAD ID - TIP MSG_EPOCH - Decay window - Hit
result - Ground track points - PH filter bbox

6.  Teams Alert

Sends card payload to TEAMS_WEBHOOK_URL including: - Object - Window -
Distance - Viewer link

7.  Dropbox Upload

Uploads JSON to: /reentry_alerts

Uses: - Refresh Token (long-term, no expiration) - App key + secret

8.  Housekeeping

Deletes: - Local JSON older than X days - Dropbox JSON older than X days

------------------------------------------------------------------------

ENVIRONMENT VARIABLES

Create a .env file:

SPACE_TRACK_USERNAME=your_user SPACE_TRACK_PASSWORD=your_pass

TEAMS_WEBHOOK_URL=https://...

DROPBOX_APP_KEY=xxxx DROPBOX_APP_SECRET=xxxx DROPBOX_REFRESH_TOKEN=xxxx
DROPBOX_FOLDER=/reentry_alerts

RETENTION_DAYS=21 TIP_LIMIT=200 NORAD_IDS=56817

------------------------------------------------------------------------

RUNNING THE SCRIPT

Run once: python monitoring.py monitor-once

Generate dummy test event: python monitoring.py dummy-alert

------------------------------------------------------------------------

BUILD WINDOWS EXECUTABLE (.exe)

pyinstaller --noconfirm --clean --onefile --name monitoring
monitoring.py

If command not found: python -m PyInstaller --noconfirm --clean
--onefile --name monitoring monitoring.py

Output: dist/monitoring.exe

------------------------------------------------------------------------

TEST THE EXECUTABLE

dist`\monitoring`{=tex}.exe monitor-once

------------------------------------------------------------------------

SCHEDULE TO RUN HOURLY (Windows)

1.  Open Task Scheduler
2.  Create Basic Task
3.  Trigger → Daily → Repeat every 1 hour
4.  Action → Start a Program
5.  Program:
    C:`\path`{=tex}`\to`{=tex}`\dist`{=tex}`\monitoring`{=tex}.exe
6.  Arguments: monitor-once

------------------------------------------------------------------------

STREAMLIT VIEWER

Run locally: streamlit run reentry_viewer_app.py

Viewer features: - Reads JSON from Dropbox - Lists events by
modification time - Shows decay window - PH bounding box - Ground
track - Hit marker - Raw JSON

------------------------------------------------------------------------

LONG-TERM TOKEN SOLUTION

Uses Refresh Token Flow. No manual token updates required. Dropbox
automatically generates short-lived access tokens from refresh token.

------------------------------------------------------------------------

COMMON ERRORS

expired_access_token\
Switch to refresh token.

missing_scope\
Enable: - files.metadata.read - files.content.read - files.content.write

Service Accounts do not have storage quota\
Do not use Google Drive service accounts unless using Shared Drive.

------------------------------------------------------------------------

Production Notes

-   TIP polling respects Space-Track limits
-   Designed for low-frequency monitoring
-   Suitable for SSA research and operational prototypes
