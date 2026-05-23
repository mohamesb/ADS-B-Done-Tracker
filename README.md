# ADS-B Drone Tracker

A live air traffic monitor with a simple rule-based anomaly classifier. Pulls aircraft positions from the [OpenSky Network](https://opensky-network.org/) every 10 seconds, plots them on a dark tactical map, and flags suspicious behavior — low altitude, slow speed, missing callsign, or erratic heading.

Built as a portfolio project for defense-tech roles. It's a miniature version of the sensor-fusion-into-operational-picture problem that air defense companies are solving.

---

## Quick start (60 seconds, no signup)

You'll need Python 3.10+.

```bash
cd adsb-tracker
python3 -m venv venv
source venv/bin/activate              # Windows: venv\Scripts\activate
pip install -r requirements.txt
USE_DEMO=1 python app.py
```

Open <http://localhost:8000>. You'll see 15 simulated aircraft, including a couple of drones and a loiterer that the classifier flags in red and yellow.

> **Tip for Windows PowerShell:** `$env:USE_DEMO=1; python app.py`

---

## How it works

```
┌──────────────────┐       ┌─────────────────────┐       ┌──────────────┐
│  OpenSky Network │ ────► │   FastAPI backend   │ ────► │   Browser    │
│   OR built-in    │       │   - polls source    │       │   - Leaflet  │
│    simulator     │       │   - classifies      │       │   - polls    │
│                  │       │   - serves JSON     │       │     /api 5s  │
└──────────────────┘       └─────────────────────┘       └──────────────┘
```

1. A background task in the backend polls a data source (OpenSky or the built-in simulator).
2. Each aircraft is run through a tiny rule-based classifier that adds `flags` and a `suspicion` score.
3. The frontend polls `/api/aircraft` every 5 seconds and re-renders the map and alert panel.

---

## The classifier (kept deliberately simple)

Each rule adds one suspicion point. The classifier is just `if` statements — no ML, no training, no model files. This is the right starting point: it's transparent, fast, and easy to defend in an interview.

| Rule | Logic | Why it matters |
|------|-------|----------------|
| Low altitude | `< 150 m` and not on ground | Airliners cruise much higher; drones live here |
| Slow speed | `< 30 m/s` (~108 km/h) and not on ground | Airliners stall below ~60 m/s; drones are slow |
| No callsign | Empty or missing | Real flights identify themselves |
| Erratic heading | `> 90°` spread across last ~6 observations | Loitering, not transit |

Suspicion score thresholds: **0 = nominal**, **1 = caution (yellow)**, **2+ = critical (red, pulsing)**.

---

## Data sources

The app tries three sources in order:

### 1. OpenSky authenticated (recommended)

OpenSky changed their API in March 2025. New accounts must use OAuth2 client credentials. Anonymous access still exists but gets 400 credits/day and often returns 403 from cloud IPs.

To enable authenticated mode:

1. Create a free account at <https://opensky-network.org/>
2. Visit your **Account** page and create a new API client
3. Set the credentials as environment variables:

```bash
export OPENSKY_CLIENT_ID=your_client_id
export OPENSKY_CLIENT_SECRET=your_client_secret
python app.py
```

You'll get 4,000 API credits/day. Tokens are auto-refreshed every 30 minutes.

### 2. OpenSky anonymous (no setup, may be blocked)

If you just run `python app.py` with no credentials, the app tries anonymous access. This works from many home IPs but is often blocked from cloud or commercial networks.

### 3. Demo simulator (always works)

Force the simulator with `USE_DEMO=1`. It generates 15 aircraft in the bounding box: 12 normal airliners, 2 drone-like targets, and 1 loiterer that wobbles its heading. Every rule of the classifier will get triggered, so it's useful for demos and screenshots.

---

## What you'll see

- **Blue triangles** — nominal aircraft (oriented by heading)
- **Yellow triangles** — one anomaly flag (caution)
- **Pulsing red triangles** — two or more flags (critical)
- **Sidebar** — live counts and a sorted list of anomalies. Click any alert to fly the map to that target and open its popup.
- **Header status** — last update time, current data source, refresh cadence

---

## Configuration

Open `app.py` and edit `BBOX` to monitor a different region:

```python
BBOX = {
    "lamin": 58.0,
    "lamax": 62.0,
    "lomin": 8.0,
    "lomax": 13.0,
}
```

Useful coordinates:

| Region | lamin | lamax | lomin | lomax |
|--------|-------|-------|-------|-------|
| Oslo / S. Norway | 58.0 | 62.0 | 8.0 | 13.0 |
| All of Norway | 57.0 | 71.5 | 4.0 | 31.5 |
| Greater London | 51.0 | 51.8 | -0.6 | 0.4 |
| New York area | 40.4 | 41.2 | -74.5 | -73.5 |

Larger bounding boxes consume more OpenSky credits per request (1–4 each). Stay under 25 square degrees to use 1 credit per call.

---

## Tuning the classifier

The rule thresholds are inside the `classify()` function in `app.py`. They're plain numbers, easy to tweak:

- Raise altitude threshold → flag more low-flying traffic
- Lower speed threshold → catch only very slow craft
- Loosen heading spread → flag straighter "loitering"

Ideas to extend it, in difficulty order:

1. Add a **squawk code** check: `7500` (hijack), `7600` (radio failure), `7700` (emergency) should all light up red.
2. Add **altitude × speed correlation** — a craft both fast and very low is weirder than either alone.
3. Replace rules with a small **isolation forest** trained on a few hours of historical data.
4. Add **geofence rules** — flag anything entering a polygon around a sensitive site.
5. Persist tracks to **SQLite** and compute features over longer time windows.

---

## What this project is NOT

It's important to be honest about limits, especially in a defense context:

- **OpenSky only sees aircraft broadcasting ADS-B.** A drone with no transponder — which is most consumer and military drones — is invisible to this system. A real counter-UAS system needs RF detection, radar, and acoustic/optical sensors on top of ADS-B.
- The classifier is a toy. It catches the easy cases. A real system needs proper tracking (Kalman filtering), multi-hypothesis association, and false-alarm tuning.
- There's no auth, no persistence, no multi-source fusion, no operator workflow. All deliberately out of scope.

Knowing what your system can't do is more impressive than pretending it does everything.

---

## File layout

```
adsb-tracker/
├── app.py              # FastAPI server, OpenSky client, simulator, classifier
├── requirements.txt    # Python deps (fastapi, uvicorn, httpx)
├── README.md           # This file
└── static/
    └── index.html      # Frontend (Leaflet + vanilla JS, dark theme)
```

Four files. No build step. Read it top to bottom in 15 minutes.

---

## License

MIT. Do whatever you want with it.
