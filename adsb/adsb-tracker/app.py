"""
ADS-B Drone Tracker
A live air traffic monitor with a simple rule-based anomaly classifier.

Data sources (in order of preference):
  1. OpenSky Network OAuth2 (set OPENSKY_CLIENT_ID + OPENSKY_CLIENT_SECRET)
  2. OpenSky Network anonymous (limited, often rate-limited)
  3. Built-in demo simulator (always works, no credentials needed)

The simulator is enabled with USE_DEMO=1, or as an automatic fallback
when OpenSky returns 403/429.
"""

import asyncio
import math
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

# Bounding box for the area we want to monitor.
# Default: Norway / Oslo region. Change these to watch somewhere else.
# Format: (min_latitude, max_latitude, min_longitude, max_longitude)
BBOX = {
    "lamin": 58.0,   # south edge
    "lamax": 62.0,   # north edge
    "lomin": 8.0,    # west edge
    "lomax": 13.0,   # east edge
}

# How often to refresh data from OpenSky (in seconds).
# OpenSky's anonymous tier is rate-limited; 10s is safe.
REFRESH_INTERVAL = 10

# OpenSky endpoints
OPENSKY_API = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/"
    "opensky-network/protocol/openid-connect/token"
)

# Credentials (optional) — set as environment variables to use authenticated mode
OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")

# Force the demo simulator on (no API calls). Useful for offline demos.
USE_DEMO = os.environ.get("USE_DEMO", "0") == "1"


# -----------------------------------------------------------------------------
# IN-MEMORY STATE
# -----------------------------------------------------------------------------

latest_data = {
    "aircraft": [],
    "updated_at": 0,
    "error": None,
    "source": "starting",   # "opensky-auth" | "opensky-anon" | "demo"
}

# Heading history per aircraft, used by the loitering rule
track_history: dict[str, list[float]] = {}
MAX_HISTORY = 6


# -----------------------------------------------------------------------------
# CLASSIFIER  --  simple rule-based, transparent and easy to defend
# -----------------------------------------------------------------------------

def classify(aircraft: dict) -> dict:
    """Run an aircraft through every rule. Adds `flags` and `suspicion` to it."""
    flags = []

    altitude = aircraft.get("baro_altitude")    # meters
    velocity = aircraft.get("velocity")          # m/s
    callsign = aircraft.get("callsign")
    on_ground = aircraft.get("on_ground")
    icao24 = aircraft.get("icao24")

    # Rule 1: Low altitude (under 150m and not on ground)
    if altitude is not None and altitude < 150 and not on_ground:
        flags.append("Low altitude")

    # Rule 2: Slow ground speed (under 30 m/s ~ 108 km/h)
    if velocity is not None and velocity < 30 and not on_ground:
        flags.append("Slow speed")

    # Rule 3: No callsign broadcast
    if not callsign or callsign.strip() == "":
        flags.append("No callsign")

    # Rule 4: Loitering (heading varies a lot across recent observations)
    # Heading is circular (0=360), so we compute the maximum pairwise
    # angular distance — the smallest arc that contains all observed headings.
    if icao24 and icao24 in track_history:
        headings = track_history[icao24]
        if len(headings) >= 4:
            max_diff = 0.0
            for i in range(len(headings)):
                for j in range(i + 1, len(headings)):
                    diff = abs(headings[i] - headings[j])
                    if diff > 180:
                        diff = 360 - diff   # take the shorter way around
                    if diff > max_diff:
                        max_diff = diff
            if max_diff > 90:
                flags.append("Erratic heading")

    aircraft["flags"] = flags
    aircraft["suspicion"] = len(flags)
    return aircraft


# -----------------------------------------------------------------------------
# OPENSKY  --  Real API access
# -----------------------------------------------------------------------------

OPENSKY_FIELDS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
    "spi", "position_source",
]


def parse_state(state: list) -> dict:
    """OpenSky returns each aircraft as a positional list. Convert to a dict."""
    return {f: state[i] if i < len(state) else None
            for i, f in enumerate(OPENSKY_FIELDS)}


class TokenManager:
    """Caches and auto-refreshes the OpenSky OAuth2 access token."""

    def __init__(self):
        self.token: Optional[str] = None
        self.expires_at: Optional[datetime] = None

    async def get_token(self) -> Optional[str]:
        if not (OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET):
            return None
        # Refresh 30 seconds before actual expiry to avoid race conditions
        if self.token and self.expires_at and datetime.now() < self.expires_at:
            return self.token
        return await self._refresh()

    async def _refresh(self) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    OPENSKY_TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": OPENSKY_CLIENT_ID,
                        "client_secret": OPENSKY_CLIENT_SECRET,
                    },
                )
                r.raise_for_status()
                data = r.json()
                self.token = data["access_token"]
                expires_in = data.get("expires_in", 1800)
                self.expires_at = datetime.now() + timedelta(seconds=expires_in - 30)
                print(f"[auth] new OpenSky token, expires in {expires_in}s")
                return self.token
        except Exception as e:
            print(f"[auth] token refresh failed: {e}")
            return None


tokens = TokenManager()


async def fetch_opensky() -> Optional[list[dict]]:
    """One call to OpenSky. Returns aircraft list, or None on failure."""
    headers = {}
    token = await tokens.get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(OPENSKY_API, params=BBOX, headers=headers)
        r.raise_for_status()
        data = r.json()

    states = data.get("states") or []
    aircraft = []
    for state in states:
        ac = parse_state(state)
        if ac["latitude"] is None or ac["longitude"] is None:
            continue
        aircraft.append(ac)

    latest_data["source"] = "opensky-auth" if token else "opensky-anon"
    return aircraft


# -----------------------------------------------------------------------------
# DEMO SIMULATOR  --  Always works, no credentials needed
# Generates a mix of normal traffic and a few suspicious tracks so the
# anomaly classifier has something interesting to find.
# -----------------------------------------------------------------------------

class SimAircraft:
    """A single simulated aircraft with realistic motion."""

    def __init__(self, kind: str, lat: float, lon: float, heading: float):
        self.icao24 = f"sim{random.randint(0x100000, 0xffffff):06x}"
        self.kind = kind  # "airliner" | "drone" | "loiterer"
        self.lat = lat
        self.lon = lon
        self.heading = heading

        if kind == "airliner":
            self.callsign = random.choice([
                "SAS451", "DY631", "BAW778", "AFR1234", "DLH8810",
                "KLM1149", "FIN8AC", "NAX72PJ",
            ])
            self.altitude = random.uniform(8000, 11500)   # m
            self.velocity = random.uniform(220, 260)       # m/s
            self.country = random.choice(["Norway", "Sweden", "Germany", "France"])
        elif kind == "drone":
            self.callsign = ""                             # no callsign
            self.altitude = random.uniform(80, 140)        # low
            self.velocity = random.uniform(15, 25)         # slow
            self.country = "Unknown"
        else:  # loiterer
            self.callsign = random.choice(["RECON1", "TEST", ""])
            self.altitude = random.uniform(200, 800)
            self.velocity = random.uniform(40, 70)
            self.country = "Norway"
            self._loiter_phase = random.uniform(0, 2 * math.pi)

    def step(self, dt: float):
        """Advance the aircraft by dt seconds."""
        # Loiterers wobble their heading
        if self.kind == "loiterer":
            self._loiter_phase += dt * 0.4
            self.heading += math.sin(self._loiter_phase) * 30 * dt
            self.heading %= 360

        # Convert heading + velocity to lat/lon change
        # rough conversion: 1 degree lat ~ 111 km
        dist_m = self.velocity * dt
        dlat = (dist_m * math.cos(math.radians(self.heading))) / 111_000
        dlon = (dist_m * math.sin(math.radians(self.heading))) / (
            111_000 * math.cos(math.radians(self.lat))
        )
        self.lat += dlat
        self.lon += dlon

        # If we leave the bbox, wrap or turn around
        if not (BBOX["lamin"] < self.lat < BBOX["lamax"] and
                BBOX["lomin"] < self.lon < BBOX["lomax"]):
            self.heading = (self.heading + 180) % 360

    def to_dict(self) -> dict:
        return {
            "icao24": self.icao24,
            "callsign": self.callsign,
            "origin_country": self.country,
            "longitude": self.lon,
            "latitude": self.lat,
            "baro_altitude": self.altitude,
            "geo_altitude": self.altitude,
            "on_ground": False,
            "velocity": self.velocity,
            "true_track": self.heading,
            "vertical_rate": 0.0,
            "squawk": None,
            "spi": False,
        }


class Simulator:
    """Manages the simulated airspace."""

    def __init__(self):
        self.last_tick = time.time()
        self.fleet: list[SimAircraft] = []
        self._populate()

    def _populate(self):
        # 12 normal airliners
        for _ in range(12):
            self.fleet.append(SimAircraft(
                kind="airliner",
                lat=random.uniform(BBOX["lamin"], BBOX["lamax"]),
                lon=random.uniform(BBOX["lomin"], BBOX["lomax"]),
                heading=random.uniform(0, 360),
            ))
        # 2 suspicious drones (will trip multiple rules)
        for _ in range(2):
            self.fleet.append(SimAircraft(
                kind="drone",
                lat=random.uniform(BBOX["lamin"], BBOX["lamax"]),
                lon=random.uniform(BBOX["lomin"], BBOX["lomax"]),
                heading=random.uniform(0, 360),
            ))
        # 1 loiterer (will trip erratic heading)
        self.fleet.append(SimAircraft(
            kind="loiterer",
            lat=random.uniform(BBOX["lamin"], BBOX["lamax"]),
            lon=random.uniform(BBOX["lomin"], BBOX["lomax"]),
            heading=random.uniform(0, 360),
        ))

    def tick(self) -> list[dict]:
        now = time.time()
        dt = now - self.last_tick
        self.last_tick = now
        for ac in self.fleet:
            ac.step(dt)
        return [ac.to_dict() for ac in self.fleet]


simulator = Simulator()


# -----------------------------------------------------------------------------
# POLLER  --  Tries real API, falls back to simulator
# -----------------------------------------------------------------------------

async def poll_once():
    """Fetch one batch of aircraft, classify, and store in latest_data."""
    aircraft = None

    if not USE_DEMO:
        try:
            aircraft = await fetch_opensky()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in (401, 403, 429):
                print(f"[poll] OpenSky {code}, falling back to demo simulator")
                aircraft = None
            else:
                latest_data["error"] = f"OpenSky HTTP {code}"
                print(f"[poll] OpenSky error: {e}")
                return
        except Exception as e:
            latest_data["error"] = str(e)
            print(f"[poll] error: {e}")
            return

    if aircraft is None:
        aircraft = simulator.tick()
        latest_data["source"] = "demo"

    # Update heading history (used by the loiter rule)
    for ac in aircraft:
        icao = ac["icao24"]
        heading = ac.get("true_track")
        if icao and heading is not None:
            history = track_history.setdefault(icao, [])
            history.append(heading)
            if len(history) > MAX_HISTORY:
                history.pop(0)

    # Classify
    aircraft = [classify(ac) for ac in aircraft]

    # Sort so suspicious aircraft render on top
    aircraft.sort(key=lambda a: a["suspicion"])

    latest_data["aircraft"] = aircraft
    latest_data["updated_at"] = int(time.time())
    latest_data["error"] = None

    flagged = sum(1 for a in aircraft if a["suspicion"] > 0)
    print(f"[poll] source={latest_data['source']} "
          f"aircraft={len(aircraft)} flagged={flagged}")


async def poller_loop():
    # Demo simulator can refresh fast; real API should not
    interval = 2 if (USE_DEMO or latest_data["source"] == "demo") else REFRESH_INTERVAL
    while True:
        await poll_once()
        # Pick the right cadence after we know the source
        interval = 2 if latest_data["source"] == "demo" else REFRESH_INTERVAL
        await asyncio.sleep(interval)


# -----------------------------------------------------------------------------
# FASTAPI APP
# -----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, title="ADS-B Drone Tracker")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/aircraft")
async def get_aircraft():
    return {
        "aircraft": latest_data["aircraft"],
        "updated_at": latest_data["updated_at"],
        "error": latest_data["error"],
        "source": latest_data["source"],
        "bbox": BBOX,
        "refresh_interval": (2 if latest_data["source"] == "demo"
                             else REFRESH_INTERVAL),
    }


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    if USE_DEMO:
        print("Mode: DEMO (USE_DEMO=1)")
    elif OPENSKY_CLIENT_ID:
        print("Mode: OpenSky authenticated (OAuth2)")
    else:
        print("Mode: OpenSky anonymous (will fall back to demo if blocked)")
    print(f"BBox: {BBOX}")
    print(f"Open http://localhost:8000")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000)
