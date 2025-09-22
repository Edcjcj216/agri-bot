# ============================================================
# main.py
# Agri-bot — Open-Meteo primary, with OWM + OpenRouter fallback
# Has: auto-loop (async), keep-alive (self-ping) in background thread,
# DB bias history, ThingsBoard push, FastAPI endpoints.
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
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "180"))   # giây giữa các lần auto-push (mặc định 3 phút)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))          # timeout HTTP
DB_FILE = os.getenv("DB_FILE", "agri_bot.db")
LAT = float(os.getenv("LAT", "10.762622"))
LON = float(os.getenv("LON", "106.660172"))
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
# Time utils
# ============================================================
def _now_local() -> datetime:
    try:
        return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()
    except Exception:
        return datetime.now()

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
# Fetchers: Open-Meteo, OWM, OpenRouter (mapping to same format)
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

    # Parse daily
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
        daily_list.append(
            {
                "date": date,
                "desc": desc,
                "max": tmax[i] if i < len(tmax) else None,
                "min": tmin[i] if i < len(tmin) else None,
                "precipitation_sum": psum[i] if i < len(psum) else None,
            }
        )

    # Parse hourly
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
        hourly_list.append(
            {
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
            }
        )
    return daily_list, hourly_list, data

def fetch_owm_and_map() -> tuple[list[dict], list[dict], dict]:
    if not OWM_API_KEY:
        return [], [], {}
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {"lat": LAT, "lon": LON, "appid": OWM_API_KEY, "units": "metric", "lang": "en"}
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"OWM fetch error: {e}")
        return [], [], {}
    hourly_list: list[dict] = []
    for item in data.get("list", []):
        try:
            dt = datetime.utcfromtimestamp(item.get("dt"))
            if LOCAL_TZ:
                dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ)
            iso = dt.isoformat()
        except Exception:
            iso = None
        main = item.get("main", {})
        weather = (item.get("weather") or [])
        wc_desc = weather[0]["description"] if weather else None
        hourly_list.append(
            {
                "time": iso,
                "temperature": main.get("temp"),
                "humidity": main.get("humidity"),
                "weather_code": None,
                "weather_short": wc_desc.title() if wc_desc else None,
                "weather_desc": wc_desc.title() if wc_desc else None,
                "precipitation": item.get("rain", {}).get("3h") if item.get("rain") else None,
                "precipitation_probability": None,
                "windspeed": item.get("wind", {}).get("speed"),
                "winddir": item.get("wind", {}).get("deg"),
            }
        )
    # aggregate daily
    daily_agg: Dict[str, Dict[str, Any]] = {}
    for h in hourly_list:
        ts = _to_local_dt(h.get("time"))
        if not ts:
            continue
        d = ts.date().isoformat()
        t = h.get("temperature")
        if d not in daily_agg:
            daily_agg[d] = {"min": t, "max": t, "desc_list": [h.get("weather_desc")]}
        else:
            if t is not None:
                if daily_agg[d]["min"] is None or t < daily_agg[d]["min"]:
                    daily_agg[d]["min"] = t
                if daily_agg[d]["max"] is None or t > daily_agg[d]["max"]:
                    daily_agg[d]["max"] = t
            daily_agg[d]["desc_list"].append(h.get("weather_desc"))
    daily_list: list[dict] = []
    for d, v in sorted(daily_agg.items()):
        descs = [x for x in v.get("desc_list") if x]
        desc = max(set(descs), key=descs.count) if descs else None
        daily_list.append({"date": d, "desc": desc, "max": v.get("max"), "min": v.get("min"), "precipitation_sum": None})
    return daily_list, hourly_list, data

def fetch_openrouter_and_map() -> tuple[list[dict], list[dict], dict]:
    if not OPENROUTER_API_KEY:
        return [], [], {}
    url = "https://api.openrouter.ai/v1/weather/forecast"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    params = {"latitude": LAT, "longitude": LON, "units": "metric", "lang": "en"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"OpenRouter fetch error: {e}")
        return [], [], {}
    hourly_raw = data.get("hourly") or data.get("hours") or data.get("data") or []
    hourly_list: list[dict] = []
    for h in hourly_raw:
        time_val = h.get("time") or h.get("datetime") or h.get("dt") or h.get("timestamp")
        iso = None
        try:
            if isinstance(time_val, (int, float)):
                dt = datetime.utcfromtimestamp(int(time_val))
                if LOCAL_TZ:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ)
                iso = dt.isoformat()
            else:
                iso = str(time_val)
        except Exception:
            iso = None
        hourly_list.append(
            {
                "time": iso,
                "temperature": h.get("temperature") or h.get("temp"),
                "humidity": h.get("humidity"),
                "weather_code": None,
                "weather_short": h.get("weather_desc") or (h.get("weather") or [{}])[0].get("description"),
                "weather_desc": h.get("weather_desc") or (h.get("weather") or [{}])[0].get("description"),
                "precipitation": h.get("precipitation"),
                "precipitation_probability": None,
                "windspeed": h.get("windspeed"),
                "winddir": h.get("winddir"),
            }
        )
    daily_agg: Dict[str, Dict[str, Any]] = {}
    for h in hourly_list:
        ts = _to_local_dt(h.get("time"))
        if not ts:
            continue
        d = ts.date().isoformat()
        t = h.get("temperature")
        if d not in daily_agg:
            daily_agg[d] = {"min": t, "max": t, "desc_list": [h.get("weather_desc")]}
        else:
            if t is not None:
                if daily_agg[d]["min"] is None or t < daily_agg[d]["min"]:
                    daily_agg[d]["min"] = t
                if daily_agg[d]["max"] is None or t > daily_agg[d]["max"]:
                    daily_agg[d]["max"] = t
            daily_agg[d]["desc_list"].append(h.get("weather_desc"))
    daily_list: list[dict] = []
    for d, v in sorted(daily_agg.items()):
        descs = [x for x in v.get("desc_list") if x]
        desc = max(set(descs), key=descs.count) if descs else None
        daily_list.append({"date": d, "desc": desc, "max": v.get("max"), "min": v.get("min"), "precipitation_sum": None})
    return daily_list, hourly_list, data

# ============================================================
# Merge data & pick next 4 hours
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
        s_comp, p_comp = start_time, p
        if p_comp.tzinfo is None and s_comp.tzinfo is not None:
            p_comp = p_comp.replace(tzinfo=s_comp.tzinfo)
        if s_comp.tzinfo is None and p_comp.tzinfo is not None:
            s_comp = s_comp.replace(tzinfo=p_comp.tzinfo)
        if p_comp >= s_comp:
            start_idx = i
            break
    if start_idx is None:
        start_idx = 0
    selected: list[dict] = []
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
    merged["location"] = existing.get("location", "An Phú, Hồ Chí Minh")
    merged["latitude"] = LAT
    merged["longitude"] = LON
    merged["meta_fetched_at"] = _now_local().isoformat()
    merged["meta_provider"] = source
    logger.info(
        f"merge done. provider={source}, start_time={start_time.isoformat()}, hour_keys={[f'hour_{i}' for i in range(1, len(selected)+1)]}"
    )
    return merged

# ============================================================
# Bias update
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
# Build payload & TB push
# ============================================================
LATEST_SENSOR: dict[str, Optional[float]] = {"illuminance": None, "avg_soil_moisture": None}

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

def sanitize_for_tb(payload: dict) -> dict:
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        if k in BANNED_KEYS:
            continue
        if v is None:
            cleaned[k] = None
        elif isinstance(v, (str, int, float, bool)):
            cleaned[k] = v
        else:
            try:
                cleaned[k] = json.dumps(v, ensure_ascii=False)
            except Exception:
                cleaned[k] = str(v)
    return cleaned

def send_to_thingsboard(data: dict):
    if not TB_DEVICE_URL:
        logger.warning("[TB SKIP] No TB token configured; skip push.")
        return None
    sanitized = sanitize_for_tb(data)
    logger.info(f"[TB PUSH] keys={list(sanitized.keys())}")
    try:
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] {r.status_code} {r.text[:200]}")
        return r
    except Exception as e:
        logger.error(f"[TB ERROR] {e}")
        return None

# ============================================================
# FastAPI app + endpoints
# ============================================================
app = FastAPI(title="Agri-Bot (Open-Meteo primary, fallback OWM/OpenRouter)")

class SensorData(BaseModel):
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    illuminance: Optional[float] = None
    avg_soil_moisture: Optional[float] = None
    battery: Optional[float] = None

@app.get("/")
def root():
    return {
        "status": "running",
        "time": _now_local().isoformat(),
        "tb_ok": bool(TB_DEVICE_URL),
        "lat": LAT,
        "lon": LON,
        "interval_s": AUTO_LOOP_INTERVAL,
    }

@app.get("/weather")
def weather_endpoint():
    merged = merge_weather_and_hours({})
    merged.pop("next_hours", None)
    return merged

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"[RX SENSOR] {data.json()}")
    if data.illuminance is not None:
        LATEST_SENSOR["illuminance"] = float(data.illuminance)
    if data.avg_soil_moisture is not None:
        LATEST_SENSOR["avg_soil_moisture"] = float(data.avg_soil_moisture)
    merged = merge_weather_and_hours({})
    selected_first = {"temperature": merged.get("hour_1_temperature"), "humidity": merged.get("hour_1_humidity")}
    bias = 0.0
    try:
        if data.temperature is not None:
            bias = update_bias_and_correct(selected_first, float(data.temperature))
    except Exception:
        pass
    merged["forecast_bias"] = bias
    merged["forecast_history_len"] = len(bias_history)
    payload = build_dashboard_payload(merged)
    for k in list(BANNED_KEYS):
        payload.pop(k, None)
    send_to_thingsboard(payload)
    return {"ok": True, "bias": bias, "saved_illum": LATEST_SENSOR["illuminance"], "saved_soil": LATEST_SENSOR["avg_soil_moisture"]}

# ============================================================
# Async auto-loop (runs in the event loop as a background task)
# ============================================================
_auto_task: Optional[asyncio.Task] = None

async def auto_loop():
    logger.info("Auto-loop started (Open-Meteo primary, fallback OWM/OpenRouter)")
    while True:
        loop_started = _now_local()
        try:
            merged = merge_weather_and_hours({})
            merged.setdefault("forecast_bias", 0.0)
            merged.setdefault("forecast_history_len", len(bias_history))
            payload = build_dashboard_payload(merged)
            for k in list(BANNED_KEYS):
                payload.pop(k, None)
            send_to_thingsboard(payload)
        except asyncio.CancelledError:
            logger.info("Auto-loop task cancelled. Exiting loop.")
            break
        except Exception as e:
            logger.error(f"[AUTO] {e}")
        next_run = loop_started + timedelta(seconds=AUTO_LOOP_INTERVAL)
        logger.info(f"Auto-loop sleep {AUTO_LOOP_INTERVAL}s, next_run≈{next_run.isoformat()}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

# ============================================================
# Keep-alive thread (self-ping) - runs in separate thread
# ============================================================
def keep_alive_thread():
    logger.info(f"Keep-alive thread started. Pinging {SELF_URL} every {KEEPALIVE_INTERVAL}s")
    while True:
        try:
            r = requests.get(SELF_URL, timeout=10)
            logger.info(f"[KEEP-ALIVE] Ping {SELF_URL} -> {r.status_code}")
        except Exception as e:
            logger.warning(f"[KEEP-ALIVE ERROR] {e}")
        time.sleep(KEEPALIVE_INTERVAL)

# ============================================================
# Startup / Shutdown events
# ============================================================
@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info(f"Startup: TB_DEVICE_URL present: {bool(TB_DEVICE_URL)}")
    global _auto_task
    # start async auto-loop
    _auto_task = asyncio.create_task(auto_loop())
    # start keep-alive thread
    try:
        t = threading.Thread(target=keep_alive_thread, name="keep-alive", daemon=True)
        t.start()
        logger.info("Keep-alive thread launched.")
    except Exception as e:
        logger.warning(f"Failed to start keep-alive thread: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutting down application...")
    global _auto_task
    if _auto_task and not _auto_task.done():
        _auto_task.cancel()
        try:
            await _auto_task
        except Exception:
            pass

# ============================================================
# CLI runner for local testing
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        log_level="info",
    )
