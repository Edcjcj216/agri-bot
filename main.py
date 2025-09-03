# main.py
# Agri-bot — Open-Meteo primary, robust timezone handling & reliable hour index selection.
import os
import time
import json
import logging
import re
import requests
import asyncio
import sqlite3
import math
import random
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque

# zoneinfo for timezone handling (preferred)
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ============== CONFIG =================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "DEOAyARAvPbZkHKFVJQa")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 600))
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))

# bias history
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
bias_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ============== MAPPINGS =================
WEATHER_CODE_MAP = {
    0: "Nắng", 1: "Nắng nhẹ", 2: "Ít mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    56: "Mưa phùn lạnh", 57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    66: "Mưa lạnh nhẹ", 67: "Mưa lạnh to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có giông", 96: "Có giông", 99: "Có giông",
}

weather_cache = {"ts": 0, "data": {}}

# ----------------- DB helpers -----------------
def init_db():
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL NOT NULL,
                observed_temp REAL NOT NULL,
                ts INTEGER NOT NULL,
                provider TEXT
            )
            """
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to init bias DB: {e}")
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
        logger.info(f"Loaded {len(rows)} bias_history samples from DB")
    except Exception as e:
        logger.warning(f"Failed to load bias history from DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def insert_history_to_db(api_temp, observed_temp, provider="open-meteo"):
    try:
        conn = sqlite3.connect(BIAS_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts, provider) VALUES (?, ?, ?, ?)",
            (float(api_temp), float(observed_temp), int(time.time()), provider)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert bias history to DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ----------------- Time / utils -----------------
def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()

def _to_local_dt(timestr):
    if not timestr:
        return None
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
    if dt and dt.tzinfo is None and ZoneInfo is not None:
        try:
            return dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        except Exception:
            return dt
    return dt

# ---------- compute daily min/max from hourly ------------
def _normalize_time_str(t):
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except Exception:
        try:
            return datetime.strptime(t, "%Y-%m-%d %H:%M")
        except Exception:
            return None

def compute_daily_min_max_from_hourly(hourly_list, target_date_str):
    temps = []
    for h in hourly_list:
        t = h.get("time")
        temp = h.get("temperature")
        if t and temp is not None:
            dt = _normalize_time_str(t)
            if dt and dt.date().isoformat() == target_date_str:
                temps.append(float(temp))
    if not temps:
        return None, None
    return round(min(temps), 1), round(max(temps), 1)

# ================== OPEN-METEO FETCHER ==================
def fetch_open_meteo():
    now = _now_local()
    yesterday = (now - timedelta(days=1)).date().isoformat()
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
        "past_days": 1,
        "forecast_days": 3
    }
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo request failed: {e}")
        return [], [], False, {}

    # parse daily
    daily_list = []
    d = data.get("daily", {})
    times = d.get("time", [])
    wc = d.get("weathercode", [])
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])
    for i in range(len(times)):
        date = times[i]
        code = wc[i] if i < len(wc) else None
        desc = WEATHER_CODE_MAP.get(code)
        daily_list.append({
            "date": date,
            "desc": desc,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None
        })

    # parse hourly
    hourly_list = []
    h = data.get("hourly", {})
    h_times = h.get("time", [])
    h_temp = h.get("temperature_2m", [])
    h_humi = h.get("relativehumidity_2m", [])
    h_code = h.get("weathercode", [])

    for i in range(len(h_times)):
        code = h_code[i] if i < len(h_code) else None
        short_desc = WEATHER_CODE_MAP.get(code)
        hourly_list.append({
            "time": h_times[i],
            "temperature": h_temp[i] if i < len(h_temp) else None,
            "humidity": h_humi[i] if i < len(h_humi) else None,
            "weather_code": code,
            "weather_short": short_desc,
            "weather_desc": short_desc
        })

    has_yesterday = any(d.get("date") == yesterday for d in daily_list)
    if not has_yesterday and hourly_list:
        ymin, ymax = compute_daily_min_max_from_hourly(hourly_list, yesterday)
        if ymin is not None or ymax is not None:
            daily_list.insert(0, {"date": yesterday, "desc": None, "max": ymax, "min": ymin})
            has_yesterday = True

    return daily_list, hourly_list, has_yesterday, data

# ================== MERGE HELPERS ==================
def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}

    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()
    now = _now_local()
    today_str = now.date().isoformat()
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()

    def find_daily_by_date(date):
        for d in daily_list:
            if d.get("date") == date:
                return d
        return {}

    weather = {
        "forecast_latitude": LAT,
        "forecast_longitude": LON,
        "forecast_fetched_at": now.isoformat(),
        "today": find_daily_by_date(today_str),
        "tomorrow": find_daily_by_date(tomorrow_str),
        "next_hours": hourly_list,
    }

    flattened = {**existing_data}
    t = weather.get("today", {})
    flattened["forecast_today_desc"] = t.get("desc")
    flattened["forecast_today_max"] = t.get("max")
    flattened["forecast_today_min"] = t.get("min")

    tt = weather.get("tomorrow", {})
    flattened["forecast_tomorrow_desc"] = tt.get("desc")
    flattened["forecast_tomorrow_max"] = tt.get("max")
    flattened["forecast_tomorrow_min"] = tt.get("min")

    # giờ kế tiếp
    parsed_times = [_to_local_dt(h.get("time")) for h in hourly_list]
    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    start_idx = next((i for i, p in enumerate(parsed_times) if p and p >= now_rounded), 0)

    next_hours = []
    for offset in range(0, 4):  # chỉ lấy 4 giờ
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        next_hours.append(hourly_list[i])

    for idx_h, h in enumerate(next_hours):
        parsed = _to_local_dt(h.get("time"))
        time_label = parsed.strftime("%H:%M") if parsed else h.get("time")
        flattened[f"forecast_hour_{idx_h}_time"] = time_label
        flattened[f"forecast_hour_{idx_h}_temp"] = h.get("temperature")
        flattened[f"forecast_hour_{idx_h}_humidity"] = h.get("humidity")
        flattened[f"forecast_hour_{idx_h}_weather"] = h.get("weather_short")

    return flattened

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ sending payload (keys: {list(data.keys())})")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.get("/weather")
def weather_endpoint():
    if time.time() - weather_cache.get("ts", 0) < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]
    res = merge_weather_and_hours(existing_data={})
    weather_cache["data"] = res
    weather_cache["ts"] = time.time()
    return res

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info("ESP32 ▶ received sensor data")
    merged = {
        **data.dict(),
        "location": "An Phú, Hồ Chí Minh"
    }
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP (simulator) ==================
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

            merged = {
                **sample,
                "location": "An Phú, Hồ Chí Minh"
            }
            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())
