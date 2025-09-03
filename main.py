# main.py
# Agri-bot — Open-Meteo + sensor, dashboard-ready keys

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

# timezone handling
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ---------------- CONFIG ----------------
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "DEOAyARAvPbZkHKFVJQa")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", 10.7639))
LON = float(os.getenv("LON", 106.6563))
LOCATION_NAME = "An Phú, Hồ Chí Minh"
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))  # 5 phút
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))

# bias history
MAX_HISTORY = 48
bias_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    illuminance: float | None = 0
    avg_soil_moisture: float | None = 0
    battery: float | None = None

# ---------------- WEATHER CODE MAP ----------------
WEATHER_CODE_MAP = {
    0: "Trời quang", 1: "Ít mây", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương muối",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhỏ", 63: "Mưa vừa", 65: "Mưa to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có giông", 96: "Có giông nhẹ", 99: "Có giông mạnh",
}

# ---------------- DB helpers ----------------
def init_db():
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL NOT NULL,
                observed_temp REAL NOT NULL,
                ts INTEGER NOT NULL,
                provider TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to init DB: {e}")
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
        logger.info(f"Loaded {len(rows)} bias samples from DB")
    except Exception as e:
        logger.warning(f"Failed to load bias DB: {e}")
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
        logger.warning(f"Failed to insert bias DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------------- TIME utils ----------------
def _now_local():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()

def _to_local_dt(timestr):
    if not timestr:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(timestr)
    except:
        try:
            dt = datetime.strptime(timestr, "%Y-%m-%d %H:%M")
        except:
            return None
    if dt.tzinfo is None and ZoneInfo:
        dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
    return dt

# ---------------- OPEN-METEO ----------------
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
        "forecast_days": 2,
        "past_days": 1
    }
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo request failed: {e}")
        return [], [], {}

    # daily
    daily_list = []
    d = data.get("daily", {})
    for i, date in enumerate(d.get("time", [])):
        daily_list.append({
            "date": date,
            "desc": WEATHER_CODE_MAP.get(d.get("weathercode", [None]*len(d.get("time")))[i]),
            "max": d.get("temperature_2m_max", [None]*len(d.get("time")))[i],
            "min": d.get("temperature_2m_min", [None]*len(d.get("time")))[i]
        })

    # hourly
    hourly_list = []
    h = data.get("hourly", {})
    times = h.get("time", [])
    temps = h.get("temperature_2m", [])
    hums = h.get("relativehumidity_2m", [])
    codes = h.get("weathercode", [])
    for i, t in enumerate(times):
        hourly_list.append({
            "time": t,
            "temperature": temps[i] if i < len(temps) else None,
            "humidity": hums[i] if i < len(hums) else None,
            "weather_code": codes[i] if i < len(codes) else None,
            "weather_short": WEATHER_CODE_MAP.get(codes[i]) if i < len(codes) else None,
            "weather_desc": WEATHER_CODE_MAP.get(codes[i]) if i < len(codes) else None
        })

    return daily_list, hourly_list, data

# ---------------- BIAS ----------------
def update_bias_and_correct(next_hours, observed_temp):
    if not next_hours:
        return 0.0
    api_now = next_hours[0].get("temperature")
    if api_now is not None and observed_temp is not None:
        bias_history.append((api_now, observed_temp))
        insert_history_to_db(api_now, observed_temp)
    if bias_history:
        diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
        bias = round(sum(diffs)/len(diffs),1)
    else:
        bias = 0.0
    return bias

# ---------------- SANITIZE ----------------
def sanitize_for_tb(payload: dict):
    sanitized = dict(payload)
    for k, v in list(sanitized.items()):
        if isinstance(v, str):
            s = re.sub(r"\([^)]*\)", "", v).strip()
            s = re.sub(r"\d+[.,]?\d*\s*(mm|km/h|°C|%|kph|m/s)", "", s, flags=re.IGNORECASE).strip()
            sanitized[k] = s if s else None
    return sanitized

# ---------------- MERGE ----------------
def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}

    daily_list, hourly_list, raw = fetch_open_meteo()
    now = _now_local()
    today_str = now.date().isoformat()
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()

    def find_daily(date):
        for d in daily_list:
            if d.get("date") == date:
                return d
        return {}

    weather = {
        "today": find_daily(today_str),
        "tomorrow": find_daily(tomorrow_str),
        "next_hours": hourly_list
    }

    merged = {**existing_data}

    # today
    merged["weather_today_desc"] = weather["today"].get("desc")
    merged["weather_today_max"] = weather["today"].get("max")
    merged["weather_today_min"] = weather["today"].get("min")
    # tomorrow
    merged["weather_tomorrow_desc"] = weather["tomorrow"].get("desc")
    merged["weather_tomorrow_max"] = weather["tomorrow"].get("max")
    merged["weather_tomorrow_min"] = weather["tomorrow"].get("min")

    # next hours
    parsed_times = [_to_local_dt(h.get("time")) for h in hourly_list]
    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    start_idx = 0
    for i, t in enumerate(parsed_times):
        if t and t >= now_rounded:
            start_idx = i
            break

    next_hours = []
    for offset in range(EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        next_hours.append(hourly_list[i])
    weather["next_hours"] = next_hours

    merged["next_hours"] = next_hours

    return merged

# ---------------- DASHBOARD MAPPING ----------------
def map_for_dashboard(data: dict):
    mapped = {}

    # vị trí
    mapped["latitude"] = data.get("latitude", LAT)
    mapped["longitude"] = data.get("longitude", LON)

    # sensor
    mapped["temperature_h"] = data.get("temperature") or data.get("temperature_h")
    mapped["humidity"] = data.get("humidity") or data.get("humidity_h")
    mapped["illuminance"] = data.get("illuminance", 0)
    mapped["avg_soil_moisture"] = data.get("avg_soil_moisture", 0)
    mapped["battery"] = data.get("battery", 0)

    # dự báo 4 giờ tới
    for i in range(4):
        h = data.get("next_hours", [])
        if i < len(h):
            hour_data = h[i]
            mapped[f"hour_{i+1}"] = _to_local_dt(hour_data.get("time")).strftime("%H:%M") if hour_data.get("time") else None
            mapped[f"hour_{i+1}_temperature"] = hour_data.get("temperature")
            mapped[f"hour_{i+1}_humidity"] = hour_data.get("humidity")
            mapped[f"hour_{i+1}_weather_desc"] = hour_data.get("weather_desc")

    # ngày mai
    mapped["weather_tomorrow_min"] = data.get("weather_tomorrow_min")
    mapped["weather_tomorrow_max"] = data.get("weather_tomorrow_max")
    mapped["humidity_tomorrow"] = data.get("humidity_tomorrow")
    mapped["weather_tomorrow_desc"] = data.get("weather_tomorrow_desc")

    # location
    mapped["location"] = data.get("location", LOCATION_NAME)

    return mapped

# ---------------- THINGSBOARD ----------------
def send_to_thingsboard(data: dict):
    try:
        sanitized = sanitize_for_tb(data)
        logger.info(f"TB ▶ sending payload (keys: {list(sanitized.keys())})")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ---------------- ROUTES ----------------
weather_cache = {"ts":0, "data":{}}

@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4]+"***"}

@app.get("/weather")
def weather_endpoint():
    if time.time() - weather_cache.get("ts",0) < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]
    raw = merge_weather_and_hours(existing_data={})
    mapped = map_for_dashboard(raw)
    weather_cache["data"] = mapped
    weather_cache["ts"] = time.time()
    return mapped

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ received data: {data.dict()}")
    merged = merge_weather_and_hours(existing_data=data.dict())
    bias = update_bias_and_correct(merged.get("next_hours", []), data.temperature)
    merged["forecast_bias"] = bias
    merged["forecast_history_len"] = len(bias_history)
    dashboard_data = map_for_dashboard(merged)
    send_to_thingsboard(dashboard_data)
    return {"received": data.dict(), "pushed": dashboard_data}

# ---------------- AUTO LOOP (simulator) ----------------
async def auto_loop():
    logger.info("Auto-loop simulator started")
    battery = 4.2
    while True:
        try:
            now = _now_local()
            hour = now.hour + now.minute/60.0
            base = 27.0
            amplitude = 6.0
            temp = base + amplitude*math.sin((hour-14)/24*2*math.pi) + random.uniform(-0
