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
def update_bias_and_correct(next_hours, observed_temp):
    global bias_history
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
    else:
        diffs = []

    if diffs:
        bias = round(sum(diffs) / len(diffs), 1)
    else:
        bias = 0.0

    return bias

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
def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}

    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()

    now = _now_local()
    yesterday_str = (now - timedelta(days=1)).date().isoformat()
    today_str = now.date().isoformat()
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()

    def find_daily_by_date(date):
        for d in daily_list:
            if d.get("date") == date:
                return d
        return {}

    weather = {
        "meta": {"latitude": LAT, "longitude": LON, "tz": TIMEZONE, "fetched_at": now.isoformat(), "source": "open-meteo"},
        "yesterday": find_daily_by_date(yesterday_str),
        "today": find_daily_by_date(today_str),
        "tomorrow": find_daily_by_date(tomorrow_str),
        "next_hours": hourly_list,
        "raw": raw
    }

    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if len(hums) >= 24:
        weather["humidity_yesterday"] = round(sum(hums[0:24]) / 24.0, 1)
    if len(hums) >= 48:
        weather["humidity_today"] = round(sum(hums[24:48]) / 24.0, 1)
    if len(hums) >= 72:
        weather["humidity_tomorrow"] = round(sum(hums[48:72]) / 24.0, 1)

    flattened = {**existing_data}

    t = weather.get("today", {}) or {}
    flattened["weather_today_desc"] = t.get("desc") if t.get("desc") is not None else None
    flattened["weather_today_max"] = t.get("max") if t.get("max") is not None else None
    flattened["weather_today_min"] = t.get("min") if t.get("min") is not None else None

    tt = weather.get("tomorrow", {}) or {}
    flattened["weather_tomorrow_desc"] = tt.get("desc") if tt.get("desc") is not None else None
    flattened["weather_tomorrow_max"] = tt.get("max") if tt.get("max") is not None else None
    flattened["weather_tomorrow_min"] = tt.get("min") if tt.get("min") is not None else None

    ty = weather.get("yesterday", {}) or {}
    flattened["weather_yesterday_desc"] = ty.get("desc")
    flattened["weather_yesterday_max"] = ty.get("max")
    flattened["weather_yesterday_min"] = ty.get("min")
    flattened["weather_yesterday_date"] = ty.get("date")

    hour_times = [h.get("time") for h in hourly_list] if hourly_list else []
    parsed_times = []
    for t in hour_times:
        p = _to_local_dt(t)
        parsed_times.append(p)

    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    start_idx = None
    try:
        for i, p in enumerate(parsed_times):
            if p is None:
                continue
            try:
                if p.tzinfo is not None and now_rounded.tzinfo is not None:
                    if p >= now_rounded:
                        start_idx = i
                        break
                elif p.tzinfo is None and now_rounded.tzinfo is None:
                    if p >= now_rounded:
                        start_idx = i
                        break
            except Exception:
                continue
    except Exception:
        start_idx = 0

    end_idx = start_idx + EXTENDED_HOURS if start_idx is not None else EXTENDED_HOURS
    weather["next_hours_window"] = hourly_list[start_idx:end_idx] if start_idx is not None else hourly_list[:EXTENDED_HOURS]

    flattened["next_hours"] = weather["next_hours_window"]
    flattened["raw_weather"] = weather["raw"]

    return flattened

# ================== THINGSBOARD PUSH ==================
def push_to_tb(payload):
    sanitized = sanitize_for_tb(payload)
    try:
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            logger.info(f"✅ Sent to ThingsBoard: {list(sanitized.keys())}")
        else:
            logger.warning(f"❌ Failed to push to TB: {r.status_code} {r.text}")
    except Exception as e:
        logger.warning(f"❌ Exception while pushing to TB: {e}")

# ================== APP ROUTES ==================
@app.get("/healthz")
def healthz():
    return {"status": "ok", "ts": int(time.time())}

@app.post("/sensor")
async def sensor_post(data: SensorData):
    obs_temp = data.temperature
    merged = merge_weather_and_hours()
    bias = update_bias_and_correct(merged.get("next_hours"), obs_temp)
    merged["bias_correction"] = bias
    push_to_tb(merged)
    return {"status": "ok", "bias_applied": bias}

# ================== MAIN LOOP ==================
async def main_loop():
    init_db()
    load_history_from_db()
    while True:
        try:
            merged = merge_weather_and_hours()
            push_to_tb(merged)
        except Exception as e:
            logger.warning(f"Error in main loop: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

if __name__ == "__main__":
    import uvicorn
    init_db()
    load_history_from_db()
    logger.info("Starting Agri-bot main.py...")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
