# main.py (compact telemetry)
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

# zoneinfo best-effort
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ========== CONFIG ==========
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 4))  # we'll publish up to 4 hours by default
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ========== MAPS (short labels) ==========
WEATHER_CODE_MAP = {
    0: "Nắng", 1: "Nắng nhẹ", 2: "Ít mây", 3: "Nhiều mây",
    45: "Sương muối", 48: "Sương muối",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có dông", 96: "Có dông", 99: "Có dông",
}
# small english->vn map in case provider uses text (kept minimal)
WEATHER_TEXT_MAP = {
    "clear": "Trời quang", "sunny": "Nắng", "partly": "Ít mây", "cloud": "Nhiều mây",
    "rain": "Có mưa", "drizzle": "Mưa phùn", "thunder": "Có dông", "storm": "Bão"
}

# ========== STATE ==========
bias_history = deque(maxlen=MAX_HISTORY)
weather_cache = {"ts": 0, "data": None}

# ========== DB helpers ==========
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

# ========== TIME UTIL ==========
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
    if dt.tzinfo is None and ZoneInfo is not None:
        try:
            return dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        except Exception:
            return dt
    return dt

def _short_weather_from_text(text):
    if not text:
        return None
    low = text.lower()
    for k, v in WEATHER_TEXT_MAP.items():
        if k in low:
            return v
    return text

# ========== FETCH OPEN-METEO ONLY (past_days=1) ==========
def fetch_open_meteo():
    now = _now_local()
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "hourly": "temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m",
        "past_days": 1,
        "forecast_days": 3,
        "timezone": TIMEZONE,
        "timeformat": "iso8601"
    }
    url = "https://api.open-meteo.com/v1/forecast"
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo fetch failed: {e}")
        return None
    return data

# ========== BUILD COMPACT RESULT ==========
def build_compact_weather(existing_obs=None):
    existing_obs = existing_obs or {}
    now = _now_local()

    # use cache
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        data = weather_cache["data"]
    else:
        data = fetch_open_meteo()
        if data:
            weather_cache["data"] = data
            weather_cache["ts"] = time.time()
        else:
            # if fetch failed and we have previous cached structured result, try to return that
            if weather_cache.get("data"):
                data = weather_cache["data"]
            else:
                data = None

    # default minimal result
    compact = {
        "temperature": existing_obs.get("temperature"),
        "humidity": existing_obs.get("humidity"),
        "battery": existing_obs.get("battery"),
        "location": existing_obs.get("location", "An Phú, Hồ Chí Minh"),
        "crop": existing_obs.get("crop", "Rau muống"),
        "forecast_bias": existing_obs.get("forecast_bias", 0.0),
        "forecast_history_len": existing_obs.get("forecast_history_len", len(bias_history))
    }

    if not data:
        return compact

    # DAILY parsing (yesterday/today/tomorrow) using returned daily arrays
    daily = data.get("daily", {})
    d_times = daily.get("time", [])
    d_codes = daily.get("weathercode", [])
    d_max = daily.get("temperature_2m_max", [])
    d_min = daily.get("temperature_2m_min", [])
    dates = d_times

    def daily_entry(idx):
        if idx < 0 or idx >= len(dates):
            return {}
        code = None
        try:
            code = int(d_codes[idx]) if idx < len(d_codes) else None
        except Exception:
            code = None
        desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        return {"date": dates[idx], "desc": desc, "max": (d_max[idx] if idx < len(d_max) else None),
                "min": (d_min[idx] if idx < len(d_min) else None)}

    # find today's index in daily.time (they are local dates)
    today_str = now.date().isoformat()
    idx_today = 0
    for i, dt in enumerate(dates):
        if dt == today_str:
            idx_today = i
            break

    yesterday = daily_entry(idx_today - 1)
    today = daily_entry(idx_today)
    tomorrow = daily_entry(idx_today + 1)

    compact["weather_yesterday_desc"] = yesterday.get("desc")
    compact["weather_yesterday_max"] = yesterday.get("max")
    compact["weather_yesterday_min"] = yesterday.get("min")
    compact["weather_yesterday_date"] = yesterday.get("date")

    compact["weather_today_desc"] = today.get("desc")
    compact["weather_today_max"] = today.get("max")
    compact["weather_today_min"] = today.get("min")

    compact["weather_tomorrow_desc"] = tomorrow.get("desc")
    compact["weather_tomorrow_max"] = tomorrow.get("max")
    compact["weather_tomorrow_min"] = tomorrow.get("min")

    # HOURLY parsing: pick first hour >= now (rounded to hour) and publish up to 4 hours
    hourly = data.get("hourly", {})
    h_times = hourly.get("time", [])
    h_temp = hourly.get("temperature_2m", [])
    h_humi = hourly.get("relativehumidity_2m", [])
    h_code = hourly.get("weathercode", [])
    h_texts = None  # open-meteo doesn't provide text; code->map used

    # parse timestamps to datetimes (local if timezone param used)
    parsed = []
    for t in h_times:
        parsed.append(_parse_iso_local(t))

    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    start_idx = None
    for i, p in enumerate(parsed):
        if p is None:
            continue
        try:
            if p.tzinfo is not None and now_rounded.tzinfo is not None:
                if p >= now_rounded:
                    start_idx = i
                    break
            elif p.tzinfo is None and now_rounded.tzinfo is not None:
                if p >= now_rounded.replace(tzinfo=None):
                    start_idx = i
                    break
            elif p.tzinfo is not None and now_rounded.tzinfo is None:
                if p.replace(tzinfo=None) >= now_rounded:
                    start_idx = i
                    break
            else:
                if p >= now_rounded:
                    start_idx = i
                    break
        except Exception:
            continue
    if start_idx is None:
        # fallback nearest
        start_idx = 0

    hours_to_publish = min(EXTENDED_HOURS, 4)
    for offset in range(hours_to_publish):
        i = start_idx + offset
        if i >= len(h_times):
            break
        label = f"hour_{offset}"
        # time label HH:MM
        time_label = None
        if parsed[i]:
            try:
                time_label = parsed[i].strftime("%H:%M")
            except Exception:
                time_label = h_times[i]
        else:
            time_label = h_times[i]
        compact[f"{label}"] = time_label
        if i < len(h_temp):
            compact[f"{label}_temperature"] = h_temp[i]
        if i < len(h_humi):
            compact[f"{label}_humidity"] = h_humi[i]
        # short weather text via code map
        code = None
        try:
            code = int(h_code[i]) if i < len(h_code) else None
        except Exception:
            code = None
        short = WEATHER_CODE_MAP.get(code) if code is not None else None
        compact[f"{label}_weather"] = short

    # humidity aggregates (best-effort)
    try:
        hum_list = [h for h in h_humi if h is not None]
        if len(hum_list) >= 24:
            compact["humidity_yesterday"] = round(sum(hum_list[0:24]) / 24.0, 1)
        if len(hum_list) >= 48:
            compact["humidity_today"] = round(sum(hum_list[24:48]) / 24.0, 1)
        if len(hum_list) >= 72:
            compact["humidity_tomorrow"] = round(sum(hum_list[48:72]) / 24.0, 1)
    except Exception:
        pass

    return compact

# ========== BIAS UPDATE ==========
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
    else:
        diffs = []
    if diffs:
        return round(sum(diffs) / len(diffs), 1)
    return 0.0

# ========== THINGSBOARD PUSH (compact) ==========
def send_to_thingsboard(payload: dict):
    try:
        # only send the compact dict as-is
        logger.info(f"TB ▶ sending payload keys: {list(payload.keys())}")
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.warning(f"TB push error: {e}")

# ========== ROUTES ==========
@app.get("/")
def root():
    return {"status": "running", "compact": True}

@app.get("/weather")
def weather_endpoint():
    # return compact weather structure (not the large raw JSON)
    res = build_compact_weather(existing_obs={})
    return res

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info("Received sensor data from device")
    # build compact weather and merge observed data
    compact = build_compact_weather(existing_obs=data.dict())
    # update bias history with next_hours if available
    next_hours = []
    for i in range(4):
        tkey = f"hour_{i}_temperature"
        if compact.get(tkey) is not None:
            next_hours.append({"temperature": compact.get(tkey)})
    bias = update_bias_and_correct(next_hours, data.temperature)
    compact["forecast_bias"] = bias
    compact["forecast_history_len"] = len(bias_history)
    # ensure basic observed included
    compact["temperature"] = data.temperature
    compact["humidity"] = data.humidity
    compact["battery"] = data.battery
    # push compact payload
    send_to_thingsboard(compact)
    return {"pushed_keys": list(compact.keys())}

# ========== AUTO LOOP (simulator) ==========
async def auto_loop():
    logger.info("Auto-loop started")
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
            sample = {"temperature": round(temp,1), "humidity": round(humi,1), "battery": round(battery,3)}
            compact = build_compact_weather(existing_obs=sample)
            # update bias with sample
            next_hours = []
            for i in range(4):
                tkey = f"hour_{i}_temperature"
                if compact.get(tkey) is not None:
                    next_hours.append({"temperature": compact.get(tkey)})
            bias = update_bias_and_correct(next_hours, sample["temperature"])
            compact["forecast_bias"] = bias
            compact["forecast_history_len"] = len(bias_history)
            compact["temperature"] = sample["temperature"]
            compact["humidity"] = sample["humidity"]
            compact["battery"] = sample["battery"]
            send_to_thingsboard(compact)
        except Exception as e:
            logger.error(f"AUTO_LOOP error: {e}")
        await asyncio.sleep( int(os.getenv("AUTO_LOOP_INTERVAL", 300)) )

@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())
