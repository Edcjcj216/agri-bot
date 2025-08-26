# main.py
"""
Agri-bot main service (Open-Meteo primary)
- Uses Open-Meteo as the primary weather provider (no API key required).
- Removed LLM/AI calls and removed external weather providers.
- Returns JSON on /weather and accepts /esp32-data posts for telemetry forwarding to ThingsBoard.
- Includes mapping from weather codes + English descriptions -> Vietnamese (WEATHER_MAP + WEATHER_CODE_MAP).
"""

import os
import time
import json
import math
import random
import logging
import requests
import asyncio
import sqlite3
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque
import re

# zoneinfo for local timezone handling
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))  # seconds
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))  # default 15 minutes
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))

# Bias correction settings (kept in-memory and persisted in SQLite)
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
bias_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 12))

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== MAPPINGS ==================
# WMO codes -> Vietnamese (retain common codes, thunder simplified)
WEATHER_CODE_MAP = {
    0: "Nắng",
    1: "Nắng nhẹ",
    2: "Ít mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    56: "Mưa phùn lạnh",
    57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa lạnh nhẹ",
    67: "Mưa lạnh to",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    95: "Có dông",
    96: "Có dông",
    99: "Có dông",
}

# English description -> Vietnamese (user-provided mapping)
WEATHER_MAP = {
    # Sun / clear
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Ít mây",
    "Cloudy": "Nhiều mây",
    "Overcast": "Âm u",

    # Rain
    "Patchy light rain": "Mưa nhẹ",
    "Patchy rain nearby": "Có mưa rải rác gần đó",
    "Light rain": "Mưa nhẹ",
    "Light rain shower": "Mưa rào nhẹ",
    "Patchy light drizzle": "Mưa phùn nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Moderate or heavy rain shower": "Mưa rào vừa hoặc to",
    "Torrential rain shower": "Mưa rất to",
    "Patchy rain possible": "Có thể có mưa",

    # Thunder
    "Thundery outbreaks possible": "Có dông",
    "Patchy light rain with thunder": "Mưa dông nhẹ",
    "Moderate or heavy rain with thunder": "Mưa dông to",

    # Storm / tropical
    "Storm": "Bão",
    "Tropical storm": "Áp thấp nhiệt đới",
}

PARTIAL_MAP = [
    (r"patchy rain nearby", "Có mưa rải rác gần đó"),
    (r"patchy.*rain", "Có mưa rải rác"),
    (r"patchy.*drizzle", "Mưa phùn nhẹ"),
    (r"light drizzle", "Mưa phùn nhẹ"),
    (r"light rain shower", "Mưa rào nhẹ"),
    (r"rain shower", "Mưa rào"),
    (r"heavy rain", "Mưa to"),
    (r"thunder", "Có dông"),
    (r"storm", "Bão"),
    (r"cloudy", "Nhiều mây"),
    (r"partly cloudy", "Ít mây"),
    (r"clear", "Trời quang"),
    (r"sunny", "Nắng"),
]

# ----------------- SQLite persistence for bias history -----------------

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
                provider TEXT,
                ts INTEGER NOT NULL
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
            "INSERT INTO bias_history (api_temp, observed_temp, provider, ts) VALUES (?, ?, ?, ?)",
            (float(api_temp), float(observed_temp), provider, int(time.time()))
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert bias history to DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ----------------- Helpers -----------------

def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()


def _mean(lst):
    return round(sum(lst) / len(lst), 1) if lst else None


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


def _nice_weather_desc(base_phrase: str, precip: float | None, precip_prob: float | None, windspeed: float | None):
    parts = []
    if base_phrase:
        parts.append(base_phrase)

    if precip_prob is not None:
        try:
            pp = int(round(float(precip_prob)))
            if pp > 0:
                parts.append(f"khả năng mưa ~{pp}%")
        except Exception:
            pass

    if precip is not None:
        try:
            p = float(precip)
            if p > 0.0:
                parts.append(f"lượng mưa ~{round(p,1)} mm")
        except Exception:
            pass

    if windspeed is not None:
        try:
            w = float(windspeed)
            if w >= 15:
                parts.append(f"gió mạnh {int(round(w))} km/h")
            elif w >= 8:
                parts.append(f"gió vừa {int(round(w))} km/h")
            elif w > 0:
                parts.append(f"gió nhẹ {int(round(w))} km/h")
        except Exception:
            pass

    if not parts:
        return base_phrase or "Không có dữ liệu"
    s = ", ".join(parts)
    return s[0].upper() + s[1:]

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
    hourly_vars = "time,temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": daily_vars,
        "hourly": hourly_vars,
        "past_days": 1,
        "forecast_days": 3,
        "timezone": TIMEZONE,
        "timeformat": "iso8601"
    }
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"Open-Meteo returned {r.status_code}: {r.text[:400]}")
            # retry without past/forecast day params if there's a format issue
            try:
                params.pop("past_days", None)
                params.pop("forecast_days", None)
                r2 = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
                r2.raise_for_status()
                data = r2.json()
            except Exception as e2:
                logger.warning(f"Open-Meteo retry failed: {e2}")
                raise
        else:
            r.raise_for_status()
            data = r.json()

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
            precip = h_prec[i] if i < len(h_prec) else None
            precip_prob = h_pp[i] if i < len(h_pp) else None
            windspeed = h_wind[i] if i < len(h_wind) else None
            nice = _nice_weather_desc(short_desc, precip, precip_prob, windspeed)
            hourly_list.append({
                "time": time_iso,
                "temperature": h_temp[i] if i < len(h_temp) else None,
                "humidity": h_humi[i] if i < len(h_humi) else None,
                "weather_desc": nice,
                "weather_code": code,
                "precipitation": precip,
                "precip_probability": precip_prob,
                "windspeed": windspeed,
                "winddir": h_wd[i] if i < len(h_wd) else None
            })

        has_yesterday = any(d.get("date") == yesterday for d in daily_list)
        return daily_list, hourly_list, has_yesterday, data

    except requests.HTTPError as he:
        logger.warning(f"Open-Meteo HTTPError: {he}")
        return [], [], False, {}
    except Exception as e:
        logger.warning(f"fetch_open_meteo unexpected error: {e}")
        return [], [], False, {}

# ================== BIAS CORRECTION ==================

def update_bias_and_correct(next_hours, observed_temp):
    global bias_history
    if not next_hours:
        return 0.0

    api_now = next_hours[0].get("temperature")
    if api_now is not None and observed_temp is not None:
        try:
            bias_history.append((api_now, observed_temp))
            insert_history_to_db(api_now, observed_temp, provider="open-meteo")
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

# ================== AI REMOVED ==================
# No LLM functions or calls — service focuses on weather + rule-based advice only.

# ================== AI HELPER (rule-based) - REMOVED (stub kept for compatibility)
#
# The original rule-based helper was removed per user request. To keep
# compatibility with existing call sites in the code, we keep a minimal
# stub that returns empty/placeholder advice fields so the rest of the
# pipeline continues to work without AI/nutrition/care text.

def get_advice(temp, humi, upcoming_weather=None):
    """Minimal stub: returns placeholder/empty advice structure.
    Replace or extend this stub later if you want custom rule-based
    guidance again.
    """
    pred = None
    try:
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    except Exception:
        pred = None
    return {
        "advice": "",
        "advice_nutrition": "",
        "advice_care": "",
        "advice_note": "",
        "prediction": pred
    }

# ================== THINGSBOARD ==================

def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ sending payload (keys: {list(data.keys())})")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== HELPERS ==================

def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}
    weather_daily, weather_hours, has_yday, raw = None, None, False, {}
    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()

    weather = {
        "meta": {"latitude": LAT, "longitude": LON, "tz": TIMEZONE, "fetched_at": _now_local().isoformat(), "source": "open-meteo"},
        "yesterday": daily_list[0] if daily_list and len(daily_list) > 0 and daily_list[0].get("date") == (datetime.now().date() - timedelta(days=1)).isoformat() else (daily_list[0] if daily_list else {}),
        "today": daily_list[1] if len(daily_list) > 1 else (daily_list[0] if daily_list else {}),
        "tomorrow": daily_list[2] if len(daily_list) > 2 else {},
        "next_hours": hourly_list,
        "humidity_yesterday": None,
        "humidity_today": None,
        "humidity_tomorrow": None
    }

    # aggregated humidity
    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if len(hums) >= 24:
        weather["humidity_yesterday"] = round(sum(hums[0:24]) / 24.0, 1)
    if len(hums) >= 48:
        weather["humidity_today"] = round(sum(hums[24:48]) / 24.0, 1)
    if len(hums) >= 72:
        weather["humidity_tomorrow"] = round(sum(hums[48:72]) / 24.0, 1)

    now = _now_local()
    flattened = {**existing_data}

    # daily
    if weather.get("today"):
        t = weather.get("today")
        if t.get("desc") is not None:
            rng = None
            if t.get("max") is not None and t.get("min") is not None:
                rng = f" ({t.get('min')}–{t.get('max')}°C)"
            flattened["weather_today_desc"] = (t.get("desc") or "") + (rng or "")
        else:
            flattened["weather_today_desc"] = None
        flattened["weather_today_max"] = t.get("max") if t.get("max") is not None else None
        flattened["weather_today_min"] = t.get("min") if t.get("min") is not None else None
    else:
        flattened["weather_today_desc"] = None
        flattened["weather_today_max"] = None
        flattened["weather_today_min"] = None

    # tomorrow
    if weather.get("tomorrow"):
        tt = weather.get("tomorrow")
        if tt.get("desc") is not None:
            rng = None
            if tt.get("max") is not None and tt.get("min") is not None:
                rng = f" ({tt.get('min')}–{tt.get('max')}°C)"
            flattened["weather_tomorrow_desc"] = (tt.get("desc") or "") + (rng or "")
        else:
            flattened["weather_tomorrow_desc"] = None
        flattened["weather_tomorrow_max"] = tt.get("max") if tt.get("max") is not None else None
        flattened["weather_tomorrow_min"] = tt.get("min") if tt.get("min") is not None else None
    else:
        flattened["weather_tomorrow_desc"] = None
        flattened["weather_tomorrow_max"] = None
        flattened["weather_tomorrow_min"] = None

    # yesterday
    if weather.get("yesterday"):
        ty = weather.get("yesterday")
        flattened["weather_yesterday_desc"] = ty.get("desc")
        flattened["weather_yesterday_max"] = ty.get("max")
        flattened["weather_yesterday_min"] = ty.get("min")
        flattened["weather_yesterday_date"] = ty.get("date")
    else:
        flattened["weather_yesterday_desc"] = None
        flattened["weather_yesterday_max"] = None
        flattened["weather_yesterday_min"] = None
        flattened["weather_yesterday_date"] = None

    # aggregated humidity
    if weather.get("humidity_today") is not None:
        flattened["humidity_today"] = weather.get("humidity_today")
    else:
        hlist = [h.get("humidity") for h in weather.get("next_hours", []) if h.get("humidity") is not None]
        flattened["humidity_today"] = round(sum(hlist)/len(hlist),1) if hlist else None
    if weather.get("humidity_tomorrow") is not None:
        flattened["humidity_tomorrow"] = weather.get("humidity_tomorrow")
    if weather.get("humidity_yesterday") is not None:
        flattened["humidity_yesterday"] = weather.get("humidity_yesterday")

    # hours
    for idx in range(0, EXTENDED_HOURS):
        h = None
        if idx < len(weather.get("next_hours", [])):
            h = weather["next_hours"][idx]
        time_label = None
        time_local_iso = None
        if h and h.get("time"):
            try:
                t = datetime.fromisoformat(h.get("time"))
                time_label = t.strftime("%H:%M")
                time_local_iso = t.isoformat()
            except Exception:
                time_label = h.get("time")
                time_local_iso = h.get("time")
        if time_label is not None:
            flattened[f"hour_{idx}"] = time_label
            flattened[f"hour_{idx}_time_local"] = time_local_iso
        temp = h.get("temperature") if h else None
        hum = h.get("humidity") if h else None
        desc = h.get("weather_desc") if h else None
        precip_prob = h.get("precip_probability") if h else None
        precip = h.get("precipitation") if h else None
        wind = h.get("windspeed") if h else None
        wdir = h.get("winddir") if h else None
        if temp is not None:
            flattened[f"hour_{idx}_temperature"] = temp
        if hum is not None:
            flattened[f"hour_{idx}_humidity"] = hum
        if precip_prob is not None:
            try:
                flattened[f"hour_{idx}_precip_probability"] = int(round(float(precip_prob)))
            except Exception:
                flattened[f"hour_{idx}_precip_probability"] = precip_prob
        if precip is not None:
            flattened[f"hour_{idx}_precipitation_mm"] = precip
        if wind is not None:
            flattened[f"hour_{idx}_windspeed"] = wind
        if wdir is not None:
            flattened[f"hour_{idx}_winddir"] = wdir
        if desc is not None:
            flattened[f"hour_{idx}_weather_short"] = desc
            flattened[f"hour_{idx}_weather_desc"] = desc

    # keep observed
    if "temperature" not in flattened:
        flattened["temperature"] = existing_data.get("temperature")
    if "humidity" not in flattened:
        flattened["humidity"] = existing_data.get("humidity")
    if "prediction" not in flattened:
        flattened["prediction"] = existing_data.get("prediction")
    if "location" not in flattened:
        flattened["location"] = existing_data.get("location")
    if "crop" not in flattened:
        flattened["crop"] = existing_data.get("crop")

    return flattened

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***", "extended_hours": EXTENDED_HOURS}

@app.get("/weather")
def weather_endpoint():
    return fetch_open_meteo()[3] if isinstance(fetch_open_meteo(), tuple) and len(fetch_open_meteo()) >= 4 else fetch_open_meteo()

@app.get("/bias")
def bias_status():
    diffs = [round(obs - api, 2) for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"bias": bias, "history_len": len(diffs)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    # Get weather
    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()
    next_hours = hourly_list

    # update bias
    bias = update_bias_and_correct(next_hours, data.temperature)

    # baseline advice
    advice_data = get_advice(data.temperature, data.humidity, upcoming_weather=next_hours)

    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }

    # attach hours and push
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP (simulator) ==================
async def auto_loop():
    logger.info("Auto-loop simulator started")
    battery = 4.2
    tick = 0
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

            daily_list, hourly_list, has_yday, raw = fetch_open_meteo()
            bias = update_bias_and_correct(hourly_list, sample["temperature"])  # update only
            advice_data = get_advice(sample["temperature"], sample["humidity"], upcoming_weather=hourly_list)

            merged = {
                **sample,
                **advice_data,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": bias,
                "forecast_history_len": len(bias_history)
            }

            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
            tick += 1
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())

# ================== NOTES ==================
# - Run with: uvicorn main:app --host 0.0.0.0 --port $PORT
# - Open-Meteo is primary provider. No API key required.
# - Adjust EXTENDED_HOURS, WEATHER_CACHE_SECONDS, LAT/LON, TIMEZONE via env if needed.
