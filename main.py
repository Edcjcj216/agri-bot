# main.py
# Agri-bot — final, Open-Meteo only, robust ThingsBoard URL/token handling, clear logging

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
    LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    LOCAL_TZ = None

# ========== CONFIG ==========
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "60"))  # seconds (default 60 for quicker test)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
DB_FILE = os.getenv("DB_FILE", "agri_bot.db")
LAT = float(os.getenv("LAT", "10.762622"))
LON = float(os.getenv("LON", "106.660172"))
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", "6"))

# ThingsBoard env: try multiple names, strip whitespace
_raw_token = (os.getenv("TB_DEMO_TOKEN") or os.getenv("TB_TOKEN") or os.getenv("TB_DEVICE_TOKEN") or "").strip()
TB_HOST = (os.getenv("TB_HOST") or os.getenv("TB_URL") or "https://thingsboard.cloud").strip().rstrip("/")
if _raw_token:
    TB_DEVICE_URL = f"{TB_HOST}/api/v1/{_raw_token}/telemetry"
else:
    TB_DEVICE_URL = None

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agri-bot")

def _mask_token(t: str):
    if not t:
        return "<empty>"
    t = t.strip()
    if len(t) <= 8:
        return t[:2] + "***"
    return t[:4] + "..." + t[-4:]

logger.info(f"[ENV] TB_HOST={TB_HOST}")
logger.info(f"[ENV] TB_TOKEN={_mask_token(_raw_token)} (len={len(_raw_token)})")
logger.info(f"[ENV] TB_DEVICE_URL set = {bool(TB_DEVICE_URL)}")
if TB_DEVICE_URL:
    logger.info(f"[ENV] TB_DEVICE_URL={TB_DEVICE_URL}")

# ========== WEATHER MAPPINGS ==========
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

# ========== DB helpers ==========
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL,
                observed_temp REAL,
                ts INTEGER,
                provider TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                temperature REAL,
                humidity REAL,
                battery REAL
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

def insert_history_to_db(api_temp, observed_temp, provider="open-meteo"):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts, provider) VALUES (?, ?, ?, ?)",
            (float(api_temp), float(observed_temp), int(time.time()), provider)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"insert_history_to_db error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def save_sensor_data(temp, hum, bat):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute("INSERT INTO sensor_data (ts, temperature, humidity, battery) VALUES (?, ?, ?, ?)",
                    (datetime.now(LOCAL_TZ).isoformat() if LOCAL_TZ else datetime.now().isoformat(), temp, hum, bat))
        conn.commit()
    except Exception as e:
        logger.warning(f"save_sensor_data error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ========== Time utilities ==========
def _now_local():
    if LOCAL_TZ:
        try:
            return datetime.now(LOCAL_TZ)
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
    if dt.tzinfo is None and LOCAL_TZ:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt

# ========== Fetch Open-Meteo ==========
def fetch_open_meteo():
    try:
        base = "https://api.open-meteo.com/v1/forecast"
        daily_vars = "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max"
        hourly_vars = "temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": daily_vars,
            "hourly": hourly_vars,
            "timezone": "auto",
            "timeformat": "iso8601",
            "past_days": 1,
            "forecast_days": 3
        }
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # parse similar to earlier code (keep minimal here)
        d = data.get("daily", {})
        h = data.get("hourly", {})
        # build lists as in original
        daily_list = []
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
        return daily_list, hourly_list, True, data
    except Exception as e:
        logger.error(f"Open-Meteo fetch error: {e}")
        return [], [], False, {}

# ========== Merge weather -> flattened telemetry ==========
def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}
    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()
    now = _now_local()
    flattened = {**existing_data}

    # daily
    today = next((d for d in daily_list if d.get("date") == now.date().isoformat()), {}) if daily_list else {}
    tomorrow = next((d for d in daily_list if d.get("date") == (now + timedelta(days=1)).date().isoformat()), {}) if daily_list else {}

    flattened["weather_today_desc"] = today.get("desc")
    flattened["weather_today_max"] = today.get("max")
    flattened["weather_today_min"] = today.get("min")
    flattened["weather_tomorrow_desc"] = tomorrow.get("desc")
    flattened["weather_tomorrow_max"] = tomorrow.get("max")
    flattened["weather_tomorrow_min"] = tomorrow.get("min")

    # choose start index robustly
    parsed_times = []
    hour_times = [h.get("time") for h in hourly_list] if hourly_list else []
    for t in hour_times:
        parsed = _to_local_dt(t)
        parsed_times.append(parsed)
    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    start_idx = 0
    try:
        for i, p in enumerate(parsed_times):
            if p is None:
                continue
            # normalize for comparison
            p_comp = p
            now_comp = now_rounded
            if p.tzinfo is None and now_comp.tzinfo is not None:
                p_comp = p.replace(tzinfo=now_comp.tzinfo)
            if now_comp.tzinfo is None and p.tzinfo is not None:
                now_comp = now_comp.replace(tzinfo=p.tzinfo)
            if p_comp >= now_comp:
                start_idx = i
                break
    except Exception as e:
        logger.warning(f"select index error: {e}")
        start_idx = 0

    next_hours = []
    for offset in range(EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        next_hours.append(hourly_list[i])

    for idx_h, h in enumerate(next_hours):
        parsed = _to_local_dt(h.get("time"))
        time_label = parsed.strftime("%H:%M") if parsed is not None else h.get("time")
        flattened[f"hour_{idx_h}"] = time_label
        flattened[f"hour_{idx_h}_time_local"] = parsed.isoformat() if parsed is not None else h.get("time")
        if h.get("temperature") is not None:
            flattened[f"hour_{idx_h}_temperature"] = h.get("temperature")
        if h.get("humidity") is not None:
            flattened[f"hour_{idx_h}_humidity"] = h.get("humidity")
        # weather desc prefer mapping
        short_label = None
        if h.get("weather_short"):
            short_label = h.get("weather_short")
        elif h.get("weather_code") is not None:
            try:
                short_label = WEATHER_CODE_MAP.get(int(h.get("weather_code")))
            except Exception:
                short_label = None
        if not short_label:
            rawdesc = h.get("weather_desc")
            short_label = rawdesc
        flattened[f"hour_{idx_h}_weather_short"] = short_label
        flattened[f"hour_{idx_h}_weather_desc"] = short_label

    # aggregated humidity (best-effort)
    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if hums:
        flattened["humidity_today"] = round(sum(hums[:24]) / min(24, len(hums)), 1) if len(hums) >= 1 else None

    # keep observed if present
    if "temperature" not in flattened:
        flattened["temperature"] = existing_data.get("temperature")
    if "humidity" not in flattened:
        flattened["humidity"] = existing_data.get("humidity")
    flattened["location"] = existing_data.get("location", "An Phú, Hồ Chí Minh")
    flattened["crop"] = existing_data.get("crop", "Rau muống")

    # expose next_hours for internal use
    flattened["next_hours"] = next_hours

    return flattened

# ========== Bias correction ==========
def update_bias_and_correct(next_hours, observed_temp):
    global bias_history
    if not next_hours or observed_temp is None:
        return 0.0
    api_now = next_hours[0].get("temperature")
    if api_now is not None:
        try:
            bias_history.append((api_now, observed_temp))
            insert_history_to_db(api_now, observed_temp)
        except Exception:
            pass
    diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    return round(sum(diffs) / len(diffs), 1) if diffs else 0.0

# ========== Sanitize ==========
def sanitize_for_tb(payload: dict):
    sanitized = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            sanitized[k] = v
        else:
            try:
                sanitized[k] = json.dumps(v, ensure_ascii=False)
            except Exception:
                sanitized[k] = str(v)
    return sanitized

# ========== ThingsBoard push (robust) ==========
def send_to_thingsboard(data: dict):
    if not TB_DEVICE_URL:
        logger.warning("[TB SKIP] No TB device token found; skipping push.")
        return None
    try:
        sanitized = sanitize_for_tb(data)
        logger.info(f"[TB PUSH] keys={list(sanitized.keys())}")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] status={r.status_code}, body={r.text}")
        return r
    except Exception as e:
        logger.error(f"[TB ERROR] {e}")
        return None

# ========== FastAPI app & endpoints ==========
from fastapi import FastAPI
app = FastAPI(title="Agri-Bot")

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

@app.get("/")
def root():
    return {"status": "running", "time": _now_local().isoformat(), "tb_ok": bool(TB_DEVICE_URL)}

@app.get("/weather")
def weather_endpoint():
    res = merge_weather_and_hours(existing_data={})
    # remove next_hours from API response for brevity
    res_copy = {k: v for k, v in res.items() if k != "next_hours"}
    return res_copy

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"[RX] Sensor received: {data}")
    save_sensor_data(data.temperature, data.humidity, data.battery)
    weather = merge_weather_and_hours(existing_data={})
    bias = update_bias_and_correct(weather.get("next_hours", []), data.temperature)
    merged = {
        **data.dict(),
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    # do not return every telemetry (could be large) — return short summary
    return {"received": data.dict(), "forecast_bias": merged.get("forecast_bias")}

# ========== Auto-loop simulator ==========
bias_history = deque(maxlen=int(os.getenv("BIAS_MAX_HISTORY", "48")))

async def auto_loop():
    logger.info("Auto-loop simulator starting")
    battery = 4.2
    while True:
        try:
            now = _now_local()
            hour = now.hour + now.minute / 60.0
            base = 27.0
            amplitude = 6.0
            temp = round(base + amplitude * math.sin((hour - 14) / 24.0 * 2 * math.pi) + random.uniform(-0.7, 0.7), 1)
            humi = round(max(20.0, min(95.0, 75 - (temp - base) * 3 + random.uniform(-5, 5))), 1)
            battery = round(max(3.3, battery - random.uniform(0.0005, 0.0025)), 3)
            sample = {"temperature": temp, "humidity": humi, "battery": battery}
            logger.info(f"[AUTO] Sample ▶ {sample}")
            save_sensor_data(temp, humi, battery)

            weather = merge_weather_and_hours(existing_data={})
            bias = update_bias_and_correct(weather.get("next_hours", []), temp)
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
            logger.error(f"[AUTO] error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def on_startup():
    init_db()
    # show current TB url masked
    logger.info(f"Startup: TB_DEVICE_URL present: {bool(TB_DEVICE_URL)}")
    asyncio.create_task(auto_loop())

# ========== Run server (optional) ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
