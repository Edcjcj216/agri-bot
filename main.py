# main.py
# Agri-bot — Open-Meteo primary, robust timezone handling & bias correction for temperature & humidity
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

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ================= CONFIG =================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))

MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
bias_history = deque(maxlen=MAX_HISTORY)
bias_humi_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ============ MAPPINGS ============
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

# ================= DB =================
def init_db():
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL,
                observed_temp REAL,
                api_humi REAL,
                observed_humi REAL,
                ts INTEGER,
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
        cur.execute(f"SELECT api_temp, observed_temp, api_humi, observed_humi FROM bias_history ORDER BY id DESC LIMIT ?", (MAX_HISTORY,))
        rows = cur.fetchall()
        rows.reverse()
        for api_t, obs_t, api_h, obs_h in rows:
            bias_history.append((float(api_t), float(obs_t)))
            bias_humi_history.append((float(api_h), float(obs_h)))
        logger.info(f"Loaded {len(rows)} bias_history samples from DB")
    except Exception as e:
        logger.warning(f"Failed to load bias history from DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def insert_history_to_db(api_temp, observed_temp, api_humi, observed_humi, provider="open-meteo"):
    try:
        conn = sqlite3.connect(BIAS_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, api_humi, observed_humi, ts, provider) VALUES (?, ?, ?, ?, ?, ?)",
            (api_temp, observed_temp, api_humi, observed_humi, int(time.time()), provider)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert bias history to DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ================= TIME UTILS =================
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

# ================== OPEN-METEO FETCH ==================
def fetch_open_meteo():
    now = _now_local()
    base = "https://api.open-meteo.com/v1/forecast"
    daily_vars = "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max"
    hourly_vars = "temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m"
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

    # daily
    daily_list = []
    d = data.get("daily", {})
    times = d.get("time", [])
    wc = d.get("weathercode", [])
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])
    psum = d.get("precipitation_sum", [])
    wmx = d.get("windspeed_10m_max", [])
    for i in range(len(times)):
        daily_list.append({
            "date": times[i],
            "desc": WEATHER_CODE_MAP.get(wc[i]) if i < len(wc) else None,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None,
            "precipitation_sum": psum[i] if i < len(psum) else None,
            "windspeed_max": wmx[i] if i < len(wmx) else None
        })

    # hourly
    hourly_list = []
    h = data.get("hourly", {})
    h_times = h.get("time", [])
    h_temp = h.get("temperature_2m", [])
    h_humi = h.get("relativehumidity_2m", [])
    h_code = h.get("weathercode", [])
    for i in range(len(h_times)):
        hourly_list.append({
            "time": h_times[i],
            "temperature": h_temp[i] if i < len(h_temp) else None,
            "humidity": h_humi[i] if i < len(h_humi) else None,
            "weather_code": h_code[i] if i < len(h_code) else None
        })

    return daily_list, hourly_list, True, data

# ================== BIAS CORRECTION ==================
def update_bias_and_correct(next_hours, observed_temp=None, observed_humi=None):
    global bias_history, bias_humi_history
    if not next_hours:
        return 0.0, 0.0

    api_temp = next_hours[0].get("temperature")
    api_humi = next_hours[0].get("humidity")

    # update histories
    if api_temp is not None and observed_temp is not None:
        bias_history.append((api_temp, observed_temp))
    if api_humi is not None and observed_humi is not None:
        bias_humi_history.append((api_humi, observed_humi))
    try:
        insert_history_to_db(api_temp, observed_temp, api_humi, observed_humi)
    except Exception:
        pass

    # compute bias
    diffs_temp = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    diffs_humi = [obs - api for api, obs in bias_humi_history if api is not None and obs is not None]

    bias_temp = round(sum(diffs_temp)/len(diffs_temp), 1) if diffs_temp else 0.0
    bias_humi = round(sum(diffs_humi)/len(diffs_humi), 1) if diffs_humi else 0.0

    return bias_temp, bias_humi

# ================== SANITIZE ==================
def sanitize_for_tb(payload: dict):
    sanitized = dict(payload)
    for k, v in list(sanitized.items()):
        if not isinstance(k, str):
            continue
        if k.endswith("_weather_desc") or k.endswith("_weather_short") or k.startswith("weather_"):
            if isinstance(v, str):
                s = v
                s = re.sub(r"\([^)]*\)", "", s).strip()
                s = re.sub(r"\d+[.,]?\d*\s*(mm|km/h|°C|%|kph|m/s)", "", s, flags=re.IGNORECASE)
                s = s.strip()
                sanitized[k] = s if s != "" else None
    return sanitized

# ================== MERGE ==================
def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}
    daily_list, hourly_list, _, _ = fetch_open_meteo()
    existing_data["daily"] = daily_list
    existing_data["next_hours"] = hourly_list[:EXTENDED_HOURS]
    # basic aggregate humidity
    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if hums:
        existing_data["humidity_today"] = round(sum(hums[:24])/min(24,len(hums)),1)
        existing_data["humidity_tomorrow"] = round(sum(hums[24:48])/min(24,len(hums[24:])),1) if len(hums) >= 48 else None
    return existing_data

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    try:
        sanitized = sanitize_for_tb(data)
        logger.info(f"TB ▶ sending payload (keys: {list(sanitized.keys())})")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***", "extended_hours": EXTENDED_HOURS}

@app.get("/weather")
def weather_endpoint():
    if time.time() - weather_cache.get("ts",0) < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]
    res = merge_weather_and_hours(existing_data={})
    weather_cache["data"] = res
    weather_cache["ts"] = time.time()
    return res

@app.get("/bias")
def bias_status():
    diffs_temp = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    diffs_humi = [obs - api for api, obs in bias_humi_history if api is not None and obs is not None]
    return {
        "bias_temp": round(sum(diffs_temp)/len(diffs_temp),1) if diffs_temp else 0.0,
        "bias_humi": round(sum(diffs_humi)/len(diffs_humi),1) if diffs_humi else 0.0
    }

@app.post("/esp32-data")
def receive_data(data: SensorData):
    weather = merge_weather_and_hours(existing_data={})
    bias_temp, bias_humi = update_bias_and_correct(weather.get("next_hours", []), data.temperature, data.humidity)
    merged = {
        **data.dict(),
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias_temp": bias_temp,
        "forecast_bias_humi": bias_humi,
        "forecast_history_len_temp": len(bias_history),
        "forecast_history_len_humi": len(bias_humi_history)
    }
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
async def auto_loop():
    logger.info("Auto-loop simulator started")
    battery = 4.2
    while True:
        try:
            now = _now_local()
            hour = now.hour + now.minute/60.0
            base = 27.0
            amplitude = 6.0
            temp = base + amplitude * math.sin((hour - 14)/24*2*math.pi) + random.uniform(-0.7,0.7)
            humi = max(20.0, min(95.0, 75 - (temp - base)*3 + random.uniform(-5,5)))
            battery = max(3.3, battery - random.uniform(0.0005,0.0025))
            sample = {"temperature": round(temp,1), "humidity": round(humi,1), "battery": round(battery,3)}
            weather = merge_weather_and_hours(existing_data={})
            bias_temp, bias_humi = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"], sample["humidity"])
            merged = {
                **sample,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias_temp": bias_temp,
                "forecast_bias_humi": bias_humi,
                "forecast_history_len_temp": len(bias_history),
                "forecast_history_len_humi": len(bias_humi_history)
            }
            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"Auto loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())
