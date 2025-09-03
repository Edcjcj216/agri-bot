# main.py
# Agri-bot — Open-Meteo primary, robust timezone handling & reliable hour index selection.
import os
import time
import logging
import re
import requests
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta

# zoneinfo
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ============== CONFIG =================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "DEOAyARAvPbZkHKFVJQa")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 600))   # seconds
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 4))            # only 4 hours (0..3)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ============== MAPPINGS (keep your translations) =================
WEATHER_CODE_MAP = {
    0: "Nắng", 1: "Nắng nhẹ", 2: "Ít mây", 3: "Nhiều mây",
    45: "Sương muối", 48: "Sương muối",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    56: "Mưa phùn lạnh", 57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    66: "Mưa lạnh nhẹ", 67: "Mưa lạnh to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có giông", 96: "Có giông", 99: "Có giông",
}

weather_cache = {"ts": 0, "data": {}}

# ----------------- Time helpers -----------------
def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()

def _to_local_dt(timestr):
    """Robust parse and attach local tz if naive."""
    if not timestr:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(timestr)
    except Exception:
        try:
            dt = datetime.strptime(timestr, "%Y-%m-%d %H:%M")
        except Exception:
            try:
                dt = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return None
    if dt is not None and dt.tzinfo is None and ZoneInfo is not None:
        try:
            return dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        except Exception:
            return dt
    return dt

def _is_gte(a: datetime, b: datetime) -> bool:
    """Return True if a >= b handling tz-aware/naive combos."""
    if a is None or b is None:
        return False
    try:
        if a.tzinfo is not None and b.tzinfo is not None:
            return a >= b
        if a.tzinfo is None and b.tzinfo is not None:
            return a >= b.replace(tzinfo=None)
        if a.tzinfo is not None and b.tzinfo is None:
            return a.replace(tzinfo=None) >= b
        return a >= b
    except Exception:
        return False

# ============== FETCH OPEN-METEO =================
def fetch_open_meteo():
    base = "https://api.open-meteo.com/v1/forecast"
    daily_vars = "weathercode,temperature_2m_max,temperature_2m_min"
    hourly_vars = "temperature_2m,relativehumidity_2m,weathercode"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": daily_vars,
        "hourly": hourly_vars,
        "timezone": TIMEZONE,
        "timeformat": "iso8601",
        "forecast_days": 3,
    }
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo request failed: {e}")
        return [], []

    # parse daily
    daily_list = []
    d = data.get("daily", {})
    times = d.get("time", [])
    wc = d.get("weathercode", [])
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])
    for i, date in enumerate(times):
        code = wc[i] if i < len(wc) else None
        daily_list.append({
            "date": date,
            "desc": WEATHER_CODE_MAP.get(code) if code is not None else None,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None,
        })

    # parse hourly
    hourly_list = []
    h = data.get("hourly", {})
    h_times = h.get("time", [])
    h_temp = h.get("temperature_2m", [])
    h_humi = h.get("relativehumidity_2m", [])
    h_code = h.get("weathercode", [])
    for i, t in enumerate(h_times):
        code = h_code[i] if i < len(h_code) else None
        hourly_list.append({
            "time": t,
            "temperature": h_temp[i] if i < len(h_temp) else None,
            "humidity": h_humi[i] if i < len(h_humi) else None,
            "weather_code": code,
            "weather_desc": WEATHER_CODE_MAP.get(code) if code is not None else None,
        })

    return daily_list, hourly_list

# ============== SANITIZE BEFORE TB PUSH =================
def sanitize_for_tb(payload: dict):
    sanitized = dict(payload)
    for k, v in list(sanitized.items()):
        if not isinstance(k, str):
            continue
        # clean weather desc keys
        if k.startswith("forecast_hour_") and (k.endswith("_weather") or k.endswith("_time")):
            if isinstance(v, str):
                s = re.sub(r"\([^)]*\)", "", v).strip()
                sanitized[k] = s if s != "" else None
        # also clean daily weather desc
        if k.startswith("forecast_") and k.endswith("_desc"):
            if isinstance(v, str):
                s = re.sub(r"\([^)]*\)", "", v).strip()
                sanitized[k] = s if s != "" else None
    return sanitized

# ============== MERGE FORECAST =================
def merge_weather_and_hours():
    daily_list, hourly_list = fetch_open_meteo()
    now = _now_local()

    # start building final payload (minimal keys)
    flattened = {}
    flattened["forecast_latitude"] = LAT
    flattened["forecast_longitude"] = LON
    flattened["forecast_fetched_at"] = now.isoformat()
    flattened["location"] = "22 An Phú"   # per your request: exact text

    # fill today / tomorrow from daily_list
    today_str = now.date().isoformat()
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()
    for d in daily_list:
        if d.get("date") == today_str:
            flattened["forecast_today_desc"] = d.get("desc")
            flattened["forecast_today_max"] = d.get("max")
            flattened["forecast_today_min"] = d.get("min")
        if d.get("date") == tomorrow_str:
            flattened["forecast_tomorrow_desc"] = d.get("desc")
            flattened["forecast_tomorrow_max"] = d.get("max")
            flattened["forecast_tomorrow_min"] = d.get("min")

    # parse hourly times
    parsed = [_to_local_dt(h.get("time")) for h in hourly_list]

    # rounding rule: if minute >= 1 => round up to next hour
    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 1:
        now_rounded = now_rounded + timedelta(hours=1)

    # find first index p >= now_rounded
    start_idx = None
    for i, p in enumerate(parsed):
        if p is None:
            continue
        if _is_gte(p, now_rounded):
            start_idx = i
            break
    if start_idx is None:
        # fallback nearest
        diffs = []
        for p in parsed:
            if p is None:
                diffs.append(float("inf"))
                continue
            try:
                delta = abs((p - now_rounded).total_seconds()) if p.tzinfo == now_rounded.tzinfo else abs(((p.replace(tzinfo=None)) - (now_rounded.replace(tzinfo=None))).total_seconds())
                diffs.append(delta)
            except Exception:
                diffs.append(float("inf"))
        if diffs and any(d != float("inf") for d in diffs):
            start_idx = int(min(range(len(diffs)), key=lambda i: diffs[i]))
        else:
            start_idx = 0

    # compose only EXTENDED_HOURS items (0..3)
    for offset in range(0, EXTENDED_HOURS):
        idx = start_idx + offset
        if idx >= len(hourly_list):
            break
        h = hourly_list[idx]
        t = parsed[idx]
        time_label = t.strftime("%H:%M") if t is not None else h.get("time")
        flattened[f"forecast_hour_{offset}_time"] = time_label
        flattened[f"forecast_hour_{offset}_temp"] = h.get("temperature")
        flattened[f"forecast_hour_{offset}_humidity"] = h.get("humidity")
        # use the WEATHER_CODE_MAP text (no invented words)
        wd = h.get("weather_desc")
        if isinstance(wd, str):
            wd = re.sub(r"\([^)]*\)", "", wd).strip()
            if wd == "":
                wd = None
        flattened[f"forecast_hour_{offset}_weather"] = wd

    return flattened

# ============== THINGSBOARD PUSH =================
def send_to_thingsboard(data: dict):
    try:
        sanitized = sanitize_for_tb(data)
        logger.info(f"TB ▶ sending payload (keys: {list(sanitized.keys())})")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ============== ROUTES =================
@app.get("/")
def root():
    return {"status": "running", "mode": "forecast-only", "extended_hours": EXTENDED_HOURS}

@app.get("/weather")
def weather_endpoint():
    # cache to reduce API calls
    if time.time() - weather_cache.get("ts", 0) < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]
    res = merge_weather_and_hours()
    weather_cache["data"] = res
    weather_cache["ts"] = time.time()
    return res

@app.post("/esp32-data")
def receive_data(data: SensorData):
    """
    We accept ESP32 sensor posts (if you still want), but we DO NOT include sensor keys in the forecast payload.
    This keeps telemetry focused on forecast_... keys only.
    """
    logger.info("ESP32 data received (used only for optional local processing).")
    forecast = merge_weather_and_hours()
    send_to_thingsboard(forecast)
    return {"received": data.dict(), "pushed": forecast}

# ============== AUTO LOOP (push forecast periodically) =================
async def auto_loop():
    logger.info("Auto-loop forecast sender started")
    while True:
        try:
            data = merge_weather_and_hours()
            send_to_thingsboard(data)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_loop())
