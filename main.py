# main.py
"""
Agri-bot main service — Open-Meteo primary (fixed).
- Fix: DO NOT include "time" in hourly parameter (avoids Open-Meteo 400).
- All *_weather_desc fields are short labels only.
- AI/advice removed. Keeps forecast_bias/history.
"""

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

# zoneinfo for timezone
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

# Bias correction settings
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
    95: "Có dông", 96: "Có dông", 99: "Có dông",
}

WEATHER_MAP = {
    "Sunny": "Nắng", "Clear": "Trời quang", "Partly cloudy": "Ít mây",
    "Cloudy": "Nhiều mây", "Overcast": "Âm u",
    "Patchy light rain": "Mưa nhẹ", "Patchy rain nearby": "Có mưa rải rác gần đó",
    "Light rain": "Mưa nhẹ", "Light rain shower": "Mưa rào nhẹ",
    "Patchy light drizzle": "Mưa phùn nhẹ", "Moderate rain": "Mưa vừa", "Heavy rain": "Mưa to",
    "Moderate or heavy rain shower": "Mưa rào vừa hoặc to", "Torrential rain shower": "Mưa rất to",
    "Patchy rain possible": "Có thể có mưa",
    "Thundery outbreaks possible": "Có dông", "Patchy light rain with thunder": "Mưa dông nhẹ",
    "Moderate or heavy rain with thunder": "Mưa dông to",
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
    (r"thunder", "Có dông"),
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

# ================== OPEN-METEO FETCHER (fixed: DO NOT include 'time' in hourly param) ==================
def fetch_open_meteo():
    now = _now_local()
    yesterday = (now - timedelta(days=1)).date().isoformat()
    base = "https://api.open-meteo.com/v1/forecast"
    daily_vars = "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max"
    # IMPORTANT: DO NOT include "time" here
    hourly_vars_full = "temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m"
    hourly_vars_simple = "temperature_2m,relativehumidity_2m,precipitation,windspeed_10m,winddirection_10m"

    base_params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": daily_vars,
        "timezone": TIMEZONE,
        "timeformat": "iso8601",
        "past_days": 1,
        "forecast_days": 3
    }

    def _do_request(hourly_vars=None, extra_params=None):
        params = dict(base_params)
        if hourly_vars:
            params["hourly"] = hourly_vars
        if extra_params:
            params.update(extra_params)
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    data = None
    try:
        # try full hourly set (WITHOUT 'time')
        try:
            data = _do_request(hourly_vars=hourly_vars_full)
        except requests.HTTPError as he:
            text = getattr(he.response, 'text', '') if hasattr(he, 'response') else str(he)
            logger.warning(f"Open-Meteo returned {getattr(he.response, 'status_code', '')}: {text[:400]}")
            data = None
        except Exception as e:
            logger.warning(f"Open-Meteo full request error: {e}")
            data = None

        # fallback: simpler hourly
        if not data:
            try:
                data = _do_request(hourly_vars=hourly_vars_simple)
            except Exception as e2:
                logger.warning(f"Open-Meteo simpler-hourly request failed: {e2}")
                data = None

        # fallback: daily-only + current_weather
        if not data:
            try:
                params_daily_only = dict(base_params)
                params_daily_only.pop('past_days', None)
                params_daily_only.pop('forecast_days', None)
                params_daily_only['daily'] = daily_vars
                params_daily_only['current_weather'] = 'true'
                r = requests.get(base, params=params_daily_only, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                data = r.json()
            except Exception as e3:
                logger.warning(f"Open-Meteo daily-only request failed: {e3}")
                data = None

        if not data:
            logger.warning("Open-Meteo: all request attempts failed; returning empty lists")
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

        # parse hourly — read hourly.time separately (we DID NOT request 'time' in hourly param)
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
                # IMPORTANT: use only short label (no numeric additions)
                "weather_short": short_desc,
                "weather_desc": short_desc,
                "precipitation": h_prec[i] if i < len(h_prec) else None,
                "precip_probability": h_pp[i] if i < len(h_pp) else None,
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
                # remove parenthetical ranges and numeric clutter
                s = re.sub(r"\([^)]*\d{1,2}[.,]?\d*°?[CF]?.*?\)", "", s)
                s = re.sub(r"\d+[.,]?\d*\s*[-–]\s*\d+[.,]?\d*°?C", "", s)
                s = re.sub(r"lượng mưa.*", "", s, flags=re.IGNORECASE)
                s = re.sub(r"gió.*", "", s, flags=re.IGNORECASE)
                s = re.sub(r"[0-9]+[.,]?[0-9]*\s*(mm|km/h|°C|%|kph|m/s)", "", s, flags=re.IGNORECASE)
                s = s.strip()
                if s == "":
                    sanitized[k] = None
                else:
                    sanitized[k] = s
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

    # aggregated humidity
    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if len(hums) >= 24:
        weather["humidity_yesterday"] = round(sum(hums[0:24]) / 24.0, 1)
    if len(hums) >= 48:
        weather["humidity_today"] = round(sum(hums[24:48]) / 24.0, 1)
    if len(hums) >= 72:
        weather["humidity_tomorrow"] = round(sum(hums[48:72]) / 24.0, 1)

    flattened = {**existing_data}

    # DAILY fields (short labels only)
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

    # find index for current hour
    hour_times = [h.get("time") for h in hourly_list] if hourly_list else []
    # convert to datetimes for matching
    idx = 0
    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    try:
        parsed_times = [_to_local_dt(t) for t in hour_times]
        # pick nearest index
        diffs = [abs((p - now_rounded).total_seconds()) if p is not None else float('inf') for p in parsed_times]
        idx = int(min(range(len(diffs)), key=lambda i: diffs[i])) if parsed_times else 0
    except Exception:
        idx = 0

    # next hours
    next_hours = []
    for offset in range(0, EXTENDED_HOURS):
        i = idx + offset
        if i >= len(hourly_list):
            break
        next_hours.append(hourly_list[i])

    # attach hours
    for idx_h, h in enumerate(next_hours):
        time_label = None
        time_local_iso = None
        if h and h.get("time"):
            parsed = _to_local_dt(h.get("time"))
            if parsed is not None:
                try:
                    time_label = parsed.strftime("%H:%M")
                    time_local_iso = parsed.isoformat()
                except Exception:
                    time_label = h.get("time")
                    time_local_iso = h.get("time")
            else:
                time_label = h.get("time")
                time_local_iso = h.get("time")
        if time_label is not None:
            flattened[f"hour_{idx_h}"] = time_label
            flattened[f"hour_{idx_h}_time_local"] = time_local_iso

        if h.get("temperature") is not None:
            flattened[f"hour_{idx_h}_temperature"] = h.get("temperature")
        if h.get("humidity") is not None:
            flattened[f"hour_{idx_h}_humidity"] = h.get("humidity")
        if h.get("precipitation") is not None:
            flattened[f"hour_{idx_h}_precipitation_mm"] = h.get("precipitation")
        if h.get("precip_probability") is not None:
            try:
                flattened[f"hour_{idx_h}_precip_probability"] = int(round(float(h.get("precip_probability"))))
            except Exception:
                flattened[f"hour_{idx_h}_precip_probability"] = h.get("precip_probability")
        if h.get("windspeed") is not None:
            flattened[f"hour_{idx_h}_windspeed"] = h.get("windspeed")
        if h.get("winddir") is not None:
            flattened[f"hour_{idx_h}_winddir"] = h.get("winddir")

        # short label only
        short_label = None
        if h.get("weather_short"):
            short_label = h.get("weather_short")
        elif h.get("weather_code") is not None:
            try:
                short_label = WEATHER_CODE_MAP.get(int(h.get("weather_code")))
            except Exception:
                short_label = None
        else:
            rawdesc = h.get("weather_desc")
            short_label = translate_desc(rawdesc) if rawdesc else None

        # clean just in case, remove numbers/parentheses
        if isinstance(short_label, str):
            short_label = re.sub(r"\([^)]*\)", "", short_label).strip()
            short_label = re.sub(r"\d+[.,]?\d*\s*(mm|km/h|°C|%|kph|m/s)", "", short_label, flags=re.IGNORECASE).strip()
            if short_label == "":
                short_label = None

        flattened[f"hour_{idx_h}_weather_short"] = short_label
        flattened[f"hour_{idx_h}_weather_desc"] = short_label

    # humidity aggregated fields
    if weather.get("humidity_today") is not None:
        flattened["humidity_today"] = weather.get("humidity_today")
    else:
        hlist = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
        flattened["humidity_today"] = round(sum(hlist)/len(hlist),1) if hlist else None
    flattened["humidity_tomorrow"] = weather.get("humidity_tomorrow")
    flattened["humidity_yesterday"] = weather.get("humidity_yesterday")

    # keep observed if present
    if "temperature" not in flattened:
        flattened["temperature"] = existing_data.get("temperature")
    if "humidity" not in flattened:
        flattened["humidity"] = existing_data.get("humidity")
    if "location" not in flattened:
        flattened["location"] = existing_data.get("location", "An Phú, Hồ Chí Minh")
    if "crop" not in flattened:
        flattened["crop"] = existing_data.get("crop", "Rau muống")

    return flattened

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
    diffs = [round(obs - api, 2) for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"bias": bias, "history_len": len(diffs)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    weather = merge_weather_and_hours(existing_data={})
    next_hours = weather.get("next_hours", [])

    bias = update_bias_and_correct(next_hours, data.temperature)

    merged = {
        **data.dict(),
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
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

            weather = merge_weather_and_hours(existing_data={})
            bias = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"])
            merged = {
                **sample,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": bias,
                "forecast_history_len": len(bias_history)
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

# ============== NOTES ============
# - Run: uvicorn main:app --host 0.0.0.0 --port $PORT
# - Important fix: do NOT include "time" in hourly param for Open-Meteo.
# - If your dashboard still shows concatenated numeric strings, update the widget to display *_weather_desc or *_weather_short.
