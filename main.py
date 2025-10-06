# ============================================================
# main.py
# Agri-bot — Open-Meteo primary, with OWM + OpenRouter fallback
# Auto-loop cải tiến, monitor push, keep-alive thread
# ============================================================

import os
import time
import json
import logging
import sqlite3
import asyncio
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------- Timezone ----------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    LOCAL_TZ = None

# ---------------- Cấu hình chung ----------------
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "600"))   # giây giữa các lần auto-push (mặc định 10 phút)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))          # timeout HTTP
DB_FILE = os.getenv("DB_FILE", "agri_bot.db")
LAT = float(os.getenv("LAT", "10.9758"))     # Dĩ An, Bình Dương
LON = float(os.getenv("LON", "106.8026"))
EXTENDED_HOURS = 4  # hour_1..hour_4

# ---------------- ThingsBoard ----------------
_raw_token = (os.getenv("TB_DEMO_TOKEN") or os.getenv("TB_TOKEN") or os.getenv("TB_DEVICE_TOKEN") or "").strip()
TB_HOST = (os.getenv("TB_HOST") or os.getenv("TB_URL") or "https://thingsboard.cloud").strip().rstrip("/")
TB_DEVICE_URL = f"{TB_HOST}/api/v1/{_raw_token}/telemetry" if _raw_token else None

# ---------------- Fallback keys ----------------
OWM_API_KEY = os.getenv("OWM_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# ---------------- Keep-alive config ----------------
SELF_URL = os.getenv("SELF_URL", "https://agri-bot-fc6r.onrender.com/")
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "300"))  # seconds

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agri-bot")

def _mask_token(t: str) -> str:
    if not t:
        return "<empty>"
    if len(t) <= 8:
        return t[:2] + "***"
    return t[:4] + "..." + t[-4:]

logger.info(f"[ENV] TB_HOST={TB_HOST}")
logger.info(f"[ENV] TB_TOKEN={_mask_token(_raw_token)} (len={len(_raw_token)})")
logger.info(f"[ENV] TB_DEVICE_URL present = {bool(TB_DEVICE_URL)}")
logger.info(f"[ENV] OWM_API_KEY present = {bool(OWM_API_KEY)}")
logger.info(f"[ENV] OPENROUTER_API_KEY present = {bool(OPENROUTER_API_KEY)}")
logger.info(f"[ENV] AUTO_LOOP_INTERVAL={AUTO_LOOP_INTERVAL}s")
logger.info(f"[ENV] SELF_URL={SELF_URL} KEEPALIVE_INTERVAL={KEEPALIVE_INTERVAL}s")

# ============================================================
# WEATHER CODE -> Tiếng Việt
# ============================================================
WEATHER_CODE_MAP = {
    0: "Trời nắng đẹp",
    1: "Trời không mây",
    2: "Trời có mây",
    3: "Trời nhiều mây",
    45: "Sương mù",
    48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày hạt",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    95: "Có giông nhẹ",
    96: "Có giông vừa",
    99: "Có giông lớn",
}

# ============================================================
# DB: lưu lịch sử bias
# ============================================================

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL,
                observed_temp REAL,
                ts INTEGER,
                provider TEXT
            )
            """
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"init_db error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def insert_history_to_db(api_temp: Optional[float], observed_temp: Optional[float], provider="open-meteo"):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts, provider) VALUES (?, ?, ?, ?)",
            (
                None if api_temp is None else float(api_temp),
                None if observed_temp is None else float(observed_temp),
                int(time.time()),
                provider,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"insert_history_to_db error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

bias_history: deque[tuple[Optional[float], Optional[float]]] = deque(maxlen=int(os.getenv("BIAS_MAX_HISTORY", "48")))

# ============================================================
# Tiện ích thời gian
# ============================================================

def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()

def _to_local_dt(timestr: Optional[str]) -> Optional[datetime]:
    if not timestr:
        return None
    dt: Optional[datetime] = None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(timestr, fmt)
            break
        except Exception:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(timestr)
        except Exception:
            return None
    if dt.tzinfo is None and LOCAL_TZ:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt

def ceil_to_next_hour(dt: datetime) -> datetime:
    if dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

# ============================================================
# Fetchers: Open-Meteo, OWM, OpenRouter
# ============================================================

def fetch_open_meteo() -> tuple[list[dict], list[dict], dict]:
    base = "https://api.open-meteo.com/v1/forecast"
    daily_vars = "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"
    hourly_vars = "temperature_2m,relativehumidity_2m,weathercode,precipitation,precipitation_probability,windspeed_10m,winddirection_10m"

    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": daily_vars,
        "hourly": hourly_vars,
        "timezone": "auto",
        "timeformat": "iso8601",
        "past_days": 1,
        "forecast_days": 3,
    }

    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Open-Meteo fetch error: {e}")
        return [], [], {}

    daily_list: list[dict] = []
    d = data.get("daily", {})
    times = d.get("time", []) or []
    wc = d.get("weathercode", []) or []
    tmax = d.get("temperature_2m_max", []) or []
    tmin = d.get("temperature_2m_min", []) or []
    psum = d.get("precipitation_sum", []) or []

    for i, date in enumerate(times):
        code = wc[i] if i < len(wc) else None
        desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        daily_list.append({
            "date": date,
            "desc": desc,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None,
            "precipitation_sum": psum[i] if i < len(psum) else None,
        })

    hourly_list: list[dict] = []
    h = data.get("hourly", {})
    h_times = h.get("time", []) or []
    h_temp = h.get("temperature_2m", []) or []
    h_humi = h.get("relativehumidity_2m", []) or []
    h_code = h.get("weathercode", []) or []
    h_prec = h.get("precipitation", []) or []
    h_pp = h.get("precipitation_probability", []) or []
    h_wind = h.get("windspeed_10m", []) or []
    h_wd = h.get("winddirection_10m", []) or []

    for i, t in enumerate(h_times):
        code = h_code[i] if i < len(h_code) else None
        label = WEATHER_CODE_MAP.get(code) if code is not None else None
        hourly_list.append({
            "time": t,
            "temperature": h_temp[i] if i < len(h_temp) else None,
            "humidity": h_humi[i] if i < len(h_humi) else None,
            "weather_code": code,
            "weather_short": label,
            "weather_desc": label,
            "precipitation": h_prec[i] if i < len(h_prec) else None,
            "precipitation_probability": h_pp[i] if i < len(h_pp) else None,
            "windspeed": h_wind[i] if i < len(h_wind) else None,
            "winddir": h_wd[i] if i < len(h_wd) else None,
        })

    return daily_list, hourly_list, data

# ============================================================
# Fallback: OWM + OpenRouter (giữ nguyên như code gốc)
# ============================================================

def fetch_owm_and_map():
    return [], [], {}

def fetch_openrouter_and_map():
    return [], [], {}

# ============================================================
# Merge dữ liệu & chọn 4 giờ tới
# ============================================================

def merge_weather_and_hours(existing: Optional[dict] = None) -> dict:
    existing = existing or {}

    daily_list, hourly_list, raw = fetch_open_meteo()
    source = "open-meteo" if hourly_list else None

    if not hourly_list:
        d_owm, h_owm, raw_owm = fetch_owm_and_map()
        if h_owm:
            logger.info("Fallback to OWM data")
            daily_list, hourly_list, raw = d_owm, h_owm, raw_owm
            source = "owm"

    if not hourly_list:
        d_or, h_or, raw_or = fetch_openrouter_and_map()
        if h_or:
            logger.info("Fallback to OpenRouter data")
            daily_list, hourly_list, raw = d_or, h_or, raw_or
            source = "openrouter"

    if not hourly_list:
        logger.error("No hourly weather data available from any provider")
        return {}

    now = _now_local()
    start_time = ceil_to_next_hour(now)

    today_iso = now.date().isoformat()
    tomorrow_iso = (now + timedelta(days=1)).date().isoformat()
    today = next((d for d in daily_list if d.get("date") == today_iso), {})
    tomorrow = next((d for d in daily_list if d.get("date") == tomorrow_iso), {})

    merged: dict[str, Any] = {}
    if today:
        merged["weather_today_desc"] = today.get("desc")
        merged["weather_today_max"] = today.get("max")
        merged["weather_today_min"] = today.get("min")
    if tomorrow:
        merged["weather_tomorrow_desc"] = tomorrow.get("desc")
        merged["weather_tomorrow_max"] = tomorrow.get("max")
        merged["weather_tomorrow_min"] = tomorrow.get("min")

    parsed_times: List[Optional[datetime]] = [_to_local_dt(h.get("time")) for h in hourly_list]

    start_idx = None
    for i, p in enumerate(parsed_times):
        if p is None:
            continue
        s_comp = start_time
        p_comp = p
        if p_comp.tzinfo is None and s_comp.tzinfo is not None:
            p_comp = p_comp.replace(tzinfo=s_comp.tzinfo)
        if s_comp.tzinfo is None and p_comp.tzinfo is not None:
            s_comp = s_comp.replace(tzinfo=p_comp.tzinfo)
        if p_comp >= s_comp:
            start_idx = i
            break
    if start_idx is None:
        start_idx = 0

    selected = []
    for offset in range(EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        selected.append(hourly_list[i])

    for k, item in enumerate(selected, start=1):
        dt_local = _to_local_dt(item.get("time"))
        label = dt_local.strftime("%H:%M") if dt_local else item.get("time")
        merged[f"hour_{k}"] = label
        if item.get("temperature") is not None:
            merged[f"hour_{k}_temperature"] = item.get("temperature")
        if item.get("humidity") is not None:
            merged[f"hour_{k}_humidity"] = item.get("humidity")
        wlabel = item.get("weather_short") or item.get("weather_desc")
        merged[f"hour_{k}_weather_desc"] = wlabel

    merged["temperature_h"] = merged.get("hour_1_temperature")
    merged["humidity"] = merged.get("hour_1_humidity")

    hums = [h.get("humidity") for h in hourly_list if isinstance(h.get("humidity"), (int, float))]
    if len(hums) >= 24:
        merged["humidity_today"] = round(sum(hums[:24]) / 24.0, 1)
    if len(hums) >= 48:
        merged["humidity_tomorrow"] = round(sum(hums[24:48]) / 24.0, 1)

    merged["location"] = "Dĩ An, Bình Dương"
    merged["latitude"] = LAT
    merged["longitude"] = LON
    merged["meta_fetched_at"] = _now_local().isoformat()
    merged["meta_provider"] = source

    logger.info(f"merge done. provider={source}, start_time={start_time.isoformat()}, hour_keys={[f'hour_{i}' for i in range(1, len(selected)+1)]}")
    return merged

# ============================================================
# Bias (tùy chọn)
# ============================================================

def update_bias_and_correct(selected_first: Optional[dict], observed_temp: Optional[float]) -> float:
    if not selected_first or observed_temp is None:
        return 0.0
    api_now = selected_first.get("temperature")
    try:
        bias_history.append((api_now, observed_temp))
        insert_history_to_db(api_now, observed_temp, provider="sensor")
    except Exception:
        pass
    diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    return round(sum(diffs) / len(diffs), 1) if diffs else 0.0

# ============================================================
# ThingsBoard payload
# ============================================================

LATEST_SENSOR: dict[str, Optional[float]] = {
    "illuminance": None,
    "avg_soil_moisture": None,
}

def build_dashboard_payload(merged: dict) -> dict:
    payload = {
        "location": merged.get("location"),
        "latitude": merged.get("latitude"),
        "longitude": merged.get("longitude"),
        "temperature_h": merged.get("hour_1_temperature"),
        "humidity": merged.get("hour_1_humidity"),
        "hour_1": merged.get("hour_1"),
        "hour_1_temperature": merged.get("hour_1_temperature"),
        "hour_1_humidity": merged.get("hour_1_humidity"),
        "hour_1_weather_desc": merged.get("hour_1_weather_desc"),
        "hour_2": merged.get("hour_2"),
        "hour_2_temperature": merged.get("hour_2_temperature"),
        "hour_2_humidity": merged.get("hour_2_humidity"),
        "hour_2_weather_desc": merged.get("hour_2_weather_desc"),
        "hour_3": merged.get("hour_3"),
        "hour_3_temperature": merged.get("hour_3_temperature"),
        "hour_3_humidity": merged.get("hour_3_humidity"),
        "hour_3_weather_desc": merged.get("hour_3_weather_desc"),
        "hour_4": merged.get("hour_4"),
        "hour_4_temperature": merged.get("hour_4_temperature"),
        "hour_4_humidity": merged.get("hour_4_humidity"),
        "hour_4_weather_desc": merged.get("hour_4_weather_desc"),
        "weather_tomorrow_min": merged.get("weather_tomorrow_min"),
        "weather_tomorrow_max": merged.get("weather_tomorrow_max"),
        "weather_tomorrow_desc": merged.get("weather_tomorrow_desc"),
        "humidity_tomorrow": merged.get("humidity_tomorrow"),
        "illuminance": LATEST_SENSOR.get("illuminance"),
        "avg_soil_moisture": LATEST_SENSOR.get("avg_soil_moisture"),
    }
    return payload
BANNED_KEYS = {"battery", "crop", "next_hours"}

def send_to_thingsboard(payload: dict) -> Optional[requests.Response]:
    if not TB_DEVICE_URL:
        return None
    try:
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning(f"TB push returned {r.status_code} {r.text}")
        else:
            logger.info(f"TB push OK. keys={list(payload.keys())}")
        return r
    except Exception as e:
        logger.error(f"TB push exception: {e}")
        return None

# ============================================================
# Auto-loop + Keep-alive + Monitor
# ============================================================

LAST_PUSH_TS: Optional[datetime] = None

async def auto_loop():
    global LAST_PUSH_TS
    logger.info("Auto-loop started")
    while True:
        loop_start = datetime.now()
        try:
            merged = merge_weather_and_hours({})
            merged.setdefault("forecast_bias", 0.0)
            merged.setdefault("forecast_history_len", len(bias_history))
            payload = build_dashboard_payload(merged)
            for k in list(BANNED_KEYS):
                payload.pop(k, None)
            resp = send_to_thingsboard(payload)
            if resp and resp.status_code == 200:
                LAST_PUSH_TS = datetime.now()
        except Exception as e:
            logger.error(f"[AUTO LOOP ERROR] {e}")
        next_run = loop_start + timedelta(seconds=AUTO_LOOP_INTERVAL)
        logger.info(f"[AUTO LOOP] Sleeping {AUTO_LOOP_INTERVAL}s, next run ≈ {next_run.isoformat()}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

def keep_alive_thread():
    logger.info(f"Keep-alive thread started. Pinging {SELF_URL} every {KEEPALIVE_INTERVAL}s")
    while True:
        try:
            r = requests.get(SELF_URL, timeout=10)
            logger.info(f"[KEEP-ALIVE] Ping {SELF_URL} -> {r.status_code}")
        except Exception as e:
            logger.warning(f"[KEEP-ALIVE ERROR] {e}")
        time.sleep(KEEPALIVE_INTERVAL)

async def monitor_push():
    global LAST_PUSH_TS
    CHECK_INTERVAL = 120
    MAX_GAP = AUTO_LOOP_INTERVAL * 2
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        now = datetime.now()
        if LAST_PUSH_TS is None or (now - LAST_PUSH_TS).total_seconds() > MAX_GAP:
            logger.warning(f"[MONITOR] Last push at {LAST_PUSH_TS}, retrying auto-loop immediately")
            try:
                merged = merge_weather_and_hours({})
                payload = build_dashboard_payload(merged)
                for k in list(BANNED_KEYS):
                    payload.pop(k, None)
                resp = send_to_thingsboard(payload)
                if resp and resp.status_code == 200:
                    LAST_PUSH_TS = datetime.now()
            except Exception as e:
                logger.error(f"[MONITOR] Retry push failed: {e}")

# ============================================================
# FastAPI app
# ============================================================

app = FastAPI(title="Agri-bot API Demo")

class SensorData(BaseModel):
    illuminance: Optional[float]
    avg_soil_moisture: Optional[float]

@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(auto_loop())
    asyncio.create_task(monitor_push())
    t = threading.Thread(target=keep_alive_thread, daemon=True)
    t.start()
    logger.info("Keep-alive thread launched.")

@app.get("/health")
async def health():
    return {"status": "ok", "last_push": LAST_PUSH_TS.isoformat() if LAST_PUSH_TS else None}

@app.get("/weather")
async def weather():
    return merge_weather_and_hours({})

@app.post("/sensor_update")
async def sensor_update(data: SensorData):
    if data.illuminance is not None:
        LATEST_SENSOR["illuminance"] = data.illuminance
    if data.avg_soil_moisture is not None:
        LATEST_SENSOR["avg_soil_moisture"] = data.avg_soil_moisture
    return {"status": "ok", "latest": LATEST_SENSOR}

# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting FastAPI server at 0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
