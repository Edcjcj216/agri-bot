# main.py - Open-Meteo only, minimal keys, hour_0 = next hour (ceil)
import os
import time
import logging
import requests
import asyncio
import sqlite3
import random
import math
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque

# zoneinfo if available
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ============ CONFIG ============
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")

EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 4))   # forecast N hours (default 4)
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))  # cache Open-Meteo 15 minutes
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))  # simulator push interval (seconds)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))

MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

# ============ LOGGING ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ============ WEATHER CODE MAP (Open-Meteo WMO codes -> Vietnamese short labels) ============
WEATHER_CODE_MAP = {
    0: "Nắng", 1: "Nắng nhẹ", 2: "Ít mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương muối",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    # Thunder replaced spelling: "Có giông"
    95: "Có giông", 96: "Có giông", 99: "Có giông",
}

# ============ STATE ============
bias_history = deque(maxlen=MAX_HISTORY)
weather_cache = {"ts": 0, "data": None}

# ============ DB helpers ============
def init_db():
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL NOT NULL,
                observed_temp REAL NOT NULL,
                ts INTEGER NOT NULL
            )
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"init_db error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def load_history_from_db():
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT api_temp, observed_temp FROM bias_history ORDER BY id DESC LIMIT ?", (MAX_HISTORY,))
        rows = cur.fetchall()
        rows.reverse()
        for api, obs in rows:
            bias_history.append((float(api), float(obs)))
        logger.info(f"Loaded {len(rows)} bias samples")
    except Exception as e:
        logger.warning(f"load_history error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def insert_history_to_db(api_temp, observed_temp):
    try:
        conn = sqlite3.connect(BIAS_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute("INSERT INTO bias_history (api_temp, observed_temp, ts) VALUES (?, ?, ?)",
                    (float(api_temp), float(observed_temp), int(time.time())))
        conn.commit()
    except Exception as e:
        logger.warning(f"insert_history error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ============ Time helpers ============
def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()

def _parse_iso_local(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        except Exception:
            return None
    # ensure timezone awareness consistent with TIMEZONE
    if dt.tzinfo is None and ZoneInfo is not None:
        try:
            return dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        except Exception:
            return dt
    return dt

# ============ Open-Meteo fetcher ============
def fetch_open_meteo():
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max",
        "hourly": "time,temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m",
        "past_days": 1,
        "forecast_days": 3,
        "timezone": TIMEZONE,
        "timeformat": "iso8601"
    }
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo fetch failed: {e}")
        return None

# ============ Build compact payload (hour_0 = next hour) ============
def build_compact_weather(existing_obs=None):
    existing_obs = existing_obs or {}
    now = _now_local()

    # cache
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        data = weather_cache["data"]
    else:
        data = fetch_open_meteo()
        if data:
            weather_cache["data"] = data
            weather_cache["ts"] = time.time()
        else:
            # fallback to last cache if exists
            if weather_cache.get("data"):
                data = weather_cache["data"]
            else:
                data = None

    compact = {
        "temperature": existing_obs.get("temperature"),
        "humidity": existing_obs.get("humidity"),
        "battery": existing_obs.get("battery"),
        "location": existing_obs.get("location", "An Phú, Hồ Chí Minh"),
        "crop": existing_obs.get("crop", "Rau muống"),
        "forecast_bias": existing_obs.get("forecast_bias", 0.0),
        "forecast_history_len": existing_obs.get("forecast_history_len", len(bias_history)),
    }

    if not data:
        return compact

    # DAILY
    daily = data.get("daily", {})
    d_times = daily.get("time", [])
    d_codes = daily.get("weathercode", [])
    d_max = daily.get("temperature_2m_max", [])
    d_min = daily.get("temperature_2m_min", [])

    today_str = now.date().isoformat()
    idx_today = 0
    for i, dt in enumerate(d_times):
        if dt == today_str:
            idx_today = i
            break

    def daily_entry(i):
        if i < 0 or i >= len(d_times):
            return {"date": None, "desc": None, "max": None, "min": None}
        code = None
        try:
            code = int(d_codes[i]) if i < len(d_codes) else None
        except Exception:
            code = None
        desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        return {"date": d_times[i], "desc": desc, "max": (d_max[i] if i < len(d_max) else None), "min": (d_min[i] if i < len(d_min) else None)}

    y = daily_entry(idx_today - 1)
    t = daily_entry(idx_today)
    tm = daily_entry(idx_today + 1)

    compact["weather_yesterday_desc"] = y.get("desc")
    compact["weather_yesterday_max"] = y.get("max")
    compact["weather_yesterday_min"] = y.get("min")
    compact["weather_yesterday_date"] = y.get("date")

    compact["weather_today_desc"] = t.get("desc")
    compact["weather_today_max"] = t.get("max")
    compact["weather_today_min"] = t.get("min")

    compact["weather_tomorrow_desc"] = tm.get("desc")
    compact["weather_tomorrow_max"] = tm.get("max")
    compact["weather_tomorrow_min"] = tm.get("min")

    # HOURLY: hour_0 is next hour (ceil)
    hourly = data.get("hourly", {})
    h_times = hourly.get("time", [])
    h_temp = hourly.get("temperature_2m", [])
    h_humi = hourly.get("relativehumidity_2m", [])
    h_code = hourly.get("weathercode", [])

    parsed = [_parse_iso_local(t) for t in h_times]

    # RULE: hour_0 = next full hour (ceil)
    now_rounded = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    start_idx = None
    for i, p in enumerate(parsed):
        if p is None:
            continue
        try:
            if p >= now_rounded:
                start_idx = i
                break
        except Exception:
            continue

    # fallback: pick closest index if not found
    if start_idx is None:
        diffs = []
        for i, p in enumerate(parsed):
            if p is None:
                continue
            try:
                diffs.append((abs((p - now_rounded).total_seconds()), i))
            except Exception:
                continue
        if diffs:
            diffs.sort(key=lambda x: x[0])
            start_idx = diffs[0][1]
        else:
            start_idx = 0

    for offset in range(min(EXTENDED_HOURS, 24)):
        i = start_idx + offset
        if i >= len(h_times):
            break
        label = f"hour_{offset}"
        time_label = parsed[i].strftime("%H:%M") if parsed[i] else h_times[i]
        compact[label] = time_label
        compact[f"{label}_temperature"] = (h_temp[i] if i < len(h_temp) else None)
        compact[f"{label}_humidity"] = (h_humi[i] if i < len(h_humi) else None)
        # short weather label only (no extra numbers)
        code = None
        try:
            code = int(h_code[i]) if i < len(h_code) else None
        except Exception:
            code = None
        compact[f"{label}_weather"] = WEATHER_CODE_MAP.get(code) if code is not None else None

    # humidity aggregates safe
    try:
        hums = [h for h in h_humi if h is not None]
        if len(hums) >= 24:
            compact["humidity_yesterday"] = round(sum(hums[0:24]) / 24.0, 1)
        if len(hums) >= 48:
            compact["humidity_today"] = round(sum(hums[24:48]) / 24.0, 1)
        if len(hums) >= 72:
            compact["humidity_tomorrow"] = round(sum(hums[48:72]) / 24.0, 1)
    except Exception:
        pass

    return compact

# ============ Bias correction (kept minimal) ============
def update_bias_and_correct(next_hours, observed_temp):
    if not next_hours:
        return 0.0
    api_now = next_hours[0].get("temperature")
    if api_now is not None and observed_temp is not None:
        try:
            bias_history.append((api_now, observed_temp))
            insert_history_to_db(api_now, observed_temp)
        except Exception:
            pass
    if bias_history:
        diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
        return round(sum(diffs) / len(diffs), 1) if diffs else 0.0
    return 0.0

# ============ ThingsBoard push (compact) ============
def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ sending keys: {list(data.keys())}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ============ ROUTES ============
@app.get("/")
def root():
    return {"status": "running", "extended_hours": EXTENDED_HOURS, "weather_cached": weather_cache.get("ts") is not None}

@app.get("/weather")
def weather_endpoint():
    return build_compact_weather()

@app.get("/bias")
def bias_status():
    diffs = [round(obs - api, 2) for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"bias": bias, "history_len": len(diffs)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info("ESP32 ▶ received sensor data")
    weather_compact = build_compact_weather(existing_obs=data.dict())
    next_hours = []
    for i in range(EXTENDED_HOURS):
        key_temp = f"hour_{i}_temperature"
        if key_temp in weather_compact:
            next_hours.append({"temperature": weather_compact.get(key_temp)})
    bias = update_bias_and_correct(next_hours, data.temperature)
    weather_compact["forecast_bias"] = bias
    weather_compact["forecast_history_len"] = len(bias_history)
    send_to_thingsboard(weather_compact)
    return {"received": data.dict(), "pushed": weather_compact}

# ============ AUTO LOOP (simulator) ============
async def auto_loop():
    logger.info("Auto-loop simulator started")
    battery = 4.2
    while True:
        try:
            now = _now_local()
            hour = now.hour + now.minute / 60.0
            base = 27.0
            amplitude = 6.0
            temp = base + amplitude * math.sin((hour - 14) / 24.0 * 2 * math.pi) + random.uniform(-0.7, 0.7)
            humi = max(20.0, min(95.0, 75 - (temp - base) * 3 + random.uniform(-5, 5)))
            battery = max(3.3, battery - random.uniform(0.0005, 0.0025))
            sample = {"temperature": round(temp, 1), "humidity": round(humi, 1), "battery": round(battery, 3)}

            merged = build_compact_weather(existing_obs=sample)
            next_hours = []
            for i in range(EXTENDED_HOURS):
                tkey = f"hour_{i}_temperature"
                next_hours.append({"temperature": merged.get(tkey)})
            bias = update_bias_and_correct(next_hours, sample["temperature"])
            merged["forecast_bias"] = bias
            merged["forecast_history_len"] = len(bias_history)
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())

# ============ NOTES ============
# - Run with: uvicorn main:app --host 0.0.0.0 --port $PORT
# - This version uses Open-Meteo only, minimal keys, hour_0 is next hour (ceil).
