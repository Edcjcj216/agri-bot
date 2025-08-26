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

# zoneinfo for timezone handling (preferred)
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ============== CONFIG =================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))
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
    45: "Sương muối", 48: "Sương muối",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    56: "Mưa phùn lạnh", 57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    66: "Mưa lạnh nhẹ", 67: "Mưa lạnh to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có giông", 96: "Có giông", 99: "Có giông",
}

WEATHER_MAP = {
    "Sunny": "Nắng", "Clear": "Trời quang", "Partly cloudy": "Ít mây",
    "Cloudy": "Nhiều mây", "Overcast": "Âm u",
    "Patchy light rain": "Mưa nhẹ", "Patchy rain nearby": "Có mưa rải rác gần đó",
    "Light rain": "Mưa nhẹ", "Light rain shower": "Mưa rào nhẹ",
    "Patchy light drizzle": "Mưa phùn nhẹ", "Moderate rain": "Mưa vừa", "Heavy rain": "Mưa to",
    "Moderate or heavy rain shower": "Mưa rào vừa hoặc to", "Torrential rain shower": "Mưa rất to",
    "Patchy rain possible": "Có thể có mưa",
    "Thundery outbreaks possible": "Có giông", "Patchy light rain with thunder": "Mưa giông nhẹ",
    "Moderate or heavy rain with thunder": "Mưa giông to",
    "Storm": "Bão", "Tropical storm": "Áp thấp nhiệt đới",
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
            bias_history.append((float(api), float(obs), float(api), float(obs)))  # humi = temp placeholder
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

def _normalize_text(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"\([^)]*\d{1,2}[.,]?\d*°?[CF]?.*?\)", "", s)
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s

PARTIAL_MAP = [
    (r"patchy rain nearby", "Có mưa rải rác gần đó"),
    (r"patchy.*rain", "Có mưa rải rác"),
    (r"patchy.*drizzle", "Mưa phùn nhẹ"),
    (r"light drizzle", "Mưa phùn nhẹ"),
    (r"light rain shower", "Mưa rào nhẹ"),
    (r"rain shower", "Mưa rào"),
    (r"heavy rain", "Mưa to"),
    (r"thunder", "Có giông"),
    (r"storm", "Bão"),
    (r"cloudy", "Nhiều mây"),
    (r"partly cloudy", "Ít mây"),
    (r"clear", "Trời quang"),
    (r"sunny", "Nắng"),
]

def translate_desc(desc_raw):
    if not desc_raw:
        return None
    cleaned = _normalize_text(desc_raw)
    if not cleaned:
        return None
    for k, v in WEATHER_MAP.items():
        if k.lower() == cleaned.lower():
            return v
    low = cleaned.lower()
    for pat, mapped in PARTIAL_MAP:
        if re.search(pat, low):
            return mapped
    return cleaned

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
            if dt is None:
                continue
            if dt.date().isoformat() == target_date_str:
                try:
                    temps.append(float(temp))
                except Exception:
                    pass
    if not temps:
        return None, None
    return round(min(temps), 1), round(max(temps), 1)

# ================== OPEN-METEO FETCHER ==================
def fetch_open_meteo():
    now = _now_local()
    yesterday = (now - timedelta(days=1)).date().isoformat()
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

    # parse daily
    daily_list = []
    d = data.get("daily", {})
    times = d.get("time", [])
    wc = d.get("weathercode", [])
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])
    psum = d.get("precipitation_sum", [])
    wmx = d.get("windspeed_10m_max", [])
    for i in range(len(times)):
        date = times[i]
        code = wc[i] if i < len(wc) else None
        desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        daily_list.append({
            "date": date,
            "desc": desc,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None,
            "precipitation_sum": psum[i] if i < len(psum) else None,
            "windspeed_max": wmx[i] if i < len(wmx) else None
        })

    # parse hourly
    hourly_list = []
    h = data.get("hourly", {})
    h_times = h.get("time", [])
    h_temp = h.get("temperature_2m", [])
    h_humi = h.get("relativehumidity_2m", [])
    h_code = h.get("weathercode", [])
    h_prec = h.get("precipitation", [])
    h_pp = h.get("precipitation_probability", [])
    h_wind = h.get("windspeed_10m", [])
    h_wd = h.get("winddirection_10m", [])

    for i in range(len(h_times)):
        time_iso = h_times[i]
        code = h_code[i] if i < len(h_code) else None
        short_desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        hourly_list.append({
            "time": time_iso,
            "temperature": h_temp[i] if i < len(h_temp) else None,
            "humidity": h_humi[i] if i < len(h_humi) else None,
            "weather_code": code,
            "weather_short": short_desc,
            "weather_desc": short_desc,
            "precipitation": h_prec[i] if i < len(h_prec) else None,
            "precipitation_probability": h_pp[i] if i < len(h_pp) else None,
            "windspeed": h_wind[i] if i < len(h_wind) else None,
            "winddir": h_wd[i] if i < len(h_wd) else None
        })

    has_yesterday = any(d.get("date") == yesterday for d in daily_list)
    if not has_yesterday and hourly_list:
        ymin, ymax = compute_daily_min_max_from_hourly(hourly_list, yesterday)
        if ymin is not None or ymax is not None:
            daily_list.insert(0, {"date": yesterday, "desc": None, "max": ymax, "min": ymin, "precipitation_sum": None, "windspeed_max": None})
            has_yesterday = True

    return daily_list, hourly_list, has_yesterday, data

# ================== BIAS CORRECTION ==================
def update_bias_and_correct(next_hours, observed_temp=None, observed_humi=None):
    global bias_history
    if not next_hours:
        return {"temperature_bias": 0.0, "humidity_bias": 0.0}

    api_now_temp = next_hours[0].get("temperature")
    api_now_humi = next_hours[0].get("humidity")

    # append to history
    if api_now_temp is not None and observed_temp is not None and api_now_humi is not None and observed_humi is not None:
        try:
            bias_history.append((api_now_temp, observed_temp, api_now_humi, observed_humi))
            insert_history_to_db(api_now_temp, observed_temp, provider="open-meteo")
        except Exception:
            pass

    # compute temperature bias
    temp_diffs = [obs - api for api, obs, _, _ in bias_history if api is not None and obs is not None]
    temperature_bias = round(sum(temp_diffs) / len(temp_diffs), 1) if temp_diffs else 0.0

    # compute humidity bias
    humi_diffs = [obs - api for _, _, api, obs in bias_history if api is not None and obs is not None]
    humidity_bias = round(sum(humi_diffs) / len(humi_diffs), 1) if humi_diffs else 0.0

    # apply bias correction to next_hours
    for h in next_hours:
        if h.get("temperature") is not None:
            h["temperature"] = round(h["temperature"] + temperature_bias, 1)
        if h.get("humidity") is not None:
            h["humidity"] = round(h["humidity"] + humidity_bias, 1)

    return {"temperature_bias": temperature_bias, "humidity_bias": humidity_bias}

# ================== SANITIZE BEFORE TB PUSH ==================
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

# ================== MERGE HELPERS ==================
# (merge_weather_and_hours function remains unchanged)
# ... [copy from your previous code]

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
    if time.time() - weather_cache.get("ts", 0) < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]
    res = merge_weather_and_hours(existing_data={})
    weather_cache["data"] = res
    weather_cache["ts"] = time.time()
    return res

@app.get("/bias")
def bias_status():
    diffs_temp = [round(obs - api, 2) for api, obs, _, _ in bias_history if api is not None and obs is not None]
    diffs_humi = [round(obs - api, 2) for _, _, api, obs in bias_history if api is not None and obs is not None]
    bias_temp = round(sum(diffs_temp) / len(diffs_temp), 2) if diffs_temp else 0.0
    bias_humi = round(sum(diffs_humi) / len(diffs_humi), 2) if diffs_humi else 0.0
    return {"temperature_bias": bias_temp, "humidity_bias": bias_humi, "history_len": len(bias_history)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    weather = merge_weather_and_hours(existing_data={})
    next_hours = weather.get("next_hours", [])

    bias_dict = update_bias_and_correct(next_hours, data.temperature, data.humidity)

    merged = {
        **data.dict(),
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_temperature_bias": bias_dict["temperature_bias"],
        "forecast_humidity_bias": bias_dict["humidity_bias"],
        "forecast_history_len": len(bias_history)
    }

    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
async def auto_loop():
    while True:
        try:
            sample = {"temperature": 30 + random.random(), "humidity": 70 + random.random(), "battery": None}
            weather = merge_weather_and_hours(existing_data={})
            bias_dict = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"], sample["humidity"])
            merged = {
                **sample,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_temperature_bias": bias_dict["temperature_bias"],
                "forecast_humidity_bias": bias_dict["humidity_bias"],
                "forecast_history_len": len(bias_history)
            }
            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
        except Exception as e:
            logger.warning(f"Auto loop failed: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

# ================== STARTUP ==================
@app.on_event("startup")
async def startup_event():
    init_db()
    load_history_from_db()
    # ping ThingsBoard with 1 telemetry to activate device
    try:
        send_to_thingsboard({"startup": int(time.time())})
    except Exception:
        pass
    asyncio.create_task(auto_loop())
