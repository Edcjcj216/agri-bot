# main.py
# Agri-bot — Open-Meteo only (FINAL)
# - Lấy forecast từ Open-Meteo
# - START HOUR = ceil to next hour (ví dụ 09:12 -> 10:00)
# - Gửi CHỈ các key dashboard cần thiết (loại bỏ battery,crop,next_hours,illuminance,avg_soil_moisture)
# - Không có dữ liệu ngẫu nhiên / simulation
# - Chú thích tiếng Việt để dễ hiểu

import os
import time
import json
import logging
import sqlite3
import asyncio
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI
from pydantic import BaseModel

# zoneinfo optional (để timezone Asia/Ho_Chi_Minh)
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    LOCAL_TZ = None

# ========== CẤU HÌNH ==========
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "600"))   # giây
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
DB_FILE = os.getenv("DB_FILE", "agri_bot.db")
LAT = float(os.getenv("LAT", "10.762622"))
LON = float(os.getenv("LON", "106.660172"))
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", "4"))  # dashboard cần 4 giờ

# ThingsBoard token/host (nếu rỗng -> skip push)
_raw_token = (os.getenv("TB_DEMO_TOKEN") or os.getenv("TB_TOKEN") or os.getenv("TB_DEVICE_TOKEN") or "").strip()
TB_HOST = (os.getenv("TB_HOST") or os.getenv("TB_URL") or "https://thingsboard.cloud").strip().rstrip("/")
TB_DEVICE_URL = f"{TB_HOST}/api/v1/{_raw_token}/telemetry" if _raw_token else None

# ========== LOGGING ==========
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
if TB_DEVICE_URL:
    logger.debug(f"[ENV] TB_DEVICE_URL={TB_DEVICE_URL}")

# ========== MAPPING THỜI TIẾT (VN) ==========
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

# ========== DB helpers (bias history minimal) ==========
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

bias_history = deque(maxlen=int(os.getenv("BIAS_MAX_HISTORY", "48")))

# ========== Time utilities ==========
def _now_local() -> datetime:
    if LOCAL_TZ:
        try:
            return datetime.now(LOCAL_TZ)
        except Exception:
            return datetime.now()
    return datetime.now()

def _to_local_dt(timestr: Optional[str]) -> Optional[datetime]:
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

def ceil_to_next_hour(dt: datetime) -> datetime:
    """Nếu dt có phút hoặc giây > 0 -> ceil lên giờ kế tiếp; nếu đúng giờ trả về chính nó."""
    if dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    return (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))

# ========== Open-Meteo fetcher ==========
def fetch_open_meteo():
    """Gọi Open-Meteo, trả về daily_list, hourly_list, has_yday, raw_json"""
    try:
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
            "forecast_days": 3
        }
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Open-Meteo fetch error: {e}")
        return [], [], False, {}

    # parse daily
    daily_list = []
    d = data.get("daily", {})
    times = d.get("time", [])
    wc = d.get("weathercode", [])
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])
    psum = d.get("precipitation_sum", [])
    for i in range(len(times)):
        date = times[i]
        code = wc[i] if i < len(wc) else None
        desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        daily_list.append({
            "date": date,
            "desc": desc,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None,
            "precipitation_sum": psum[i] if i < len(psum) else None
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

    return daily_list, hourly_list, True, data

# ========== Merge and select hours (CEIL logic) ==========
def merge_weather_and_hours(existing_data: Optional[dict] = None) -> dict:
    """
    Trả về flattened dict:
    - Chọn start_time = ceil_to_next_hour(now_local)
    - Chọn EXTENDED_HOURS bắt đầu từ start_time
    - Trả về các key hour_0..hour_{EXTENDED_HOURS-1}, hour_X_temperature/humidity/weather_desc
    - Giữ next_hours nội bộ (không push)
    """
    if existing_data is None:
        existing_data = {}
    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()
    now = _now_local()
    start_time = ceil_to_next_hour(now)  # quan trọng: làm tròn lên
    flattened = {**existing_data}

    # map daily today/tomorrow
    today_iso = now.date().isoformat()
    tomorrow_iso = (now + timedelta(days=1)).date().isoformat()
    today = next((d for d in daily_list if d.get("date") == today_iso), {}) if daily_list else {}
    tomorrow = next((d for d in daily_list if d.get("date") == tomorrow_iso), {}) if daily_list else {}

    if today:
        flattened["weather_today_desc"] = today.get("desc")
        flattened["weather_today_max"] = today.get("max")
        flattened["weather_today_min"] = today.get("min")
    if tomorrow:
        flattened["weather_tomorrow_desc"] = tomorrow.get("desc")
        flattened["weather_tomorrow_max"] = tomorrow.get("max")
        flattened["weather_tomorrow_min"] = tomorrow.get("min")

    # parse hourly times
    parsed_times: List[Optional[datetime]] = []
    for t in [h.get("time") for h in hourly_list]:
        parsed_times.append(_to_local_dt(t))

    # find index where parsed_time >= start_time
    start_idx = None
    try:
        for i, p in enumerate(parsed_times):
            if p is None:
                continue
            # normalize tz-awareness for fair compare
            p_comp = p
            s_comp = start_time
            if p_comp.tzinfo is None and s_comp.tzinfo is not None:
                p_comp = p_comp.replace(tzinfo=s_comp.tzinfo)
            if s_comp.tzinfo is None and p_comp.tzinfo is not None:
                s_comp = s_comp.replace(tzinfo=p_comp.tzinfo)
            if p_comp >= s_comp:
                start_idx = i
                break
    except Exception as e:
        logger.warning(f"start_idx selection error: {e}")
        start_idx = None

    if start_idx is None:
        # fallback nearest (should rarely happen)
        start_idx = 0

    # compose next hours starting at start_idx
    next_hours = []
    for offset in range(0, EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        next_hours.append(hourly_list[i])

    # fill flattened with hour_{i} etc.
    for idx_h, h in enumerate(next_hours):
        parsed = _to_local_dt(h.get("time"))
        time_label = parsed.strftime("%H:%M") if parsed is not None else h.get("time")
        time_iso = parsed.isoformat() if parsed is not None else h.get("time")
        flattened[f"hour_{idx_h}"] = time_label
        flattened[f"hour_{idx_h}_time_local"] = time_iso
        if h.get("temperature") is not None:
            flattened[f"hour_{idx_h}_temperature"] = h.get("temperature")
        if h.get("humidity") is not None:
            flattened[f"hour_{idx_h}_humidity"] = h.get("humidity")
        short_label = h.get("weather_short") or (WEATHER_CODE_MAP.get(int(h.get("weather_code"))) if h.get("weather_code") is not None else None)
        if not short_label:
            short_label = h.get("weather_desc")
        flattened[f"hour_{idx_h}_weather_short"] = short_label
        flattened[f"hour_{idx_h}_weather_desc"] = short_label

    # aggregated humidity best-effort
    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if hums:
        flattened["humidity_today"] = round(sum(hums[:24]) / min(24, len(hums)), 1)
    if len(hums) >= 48:
        flattened["humidity_tomorrow"] = round(sum(hums[24:48]) / 24.0, 1)

    # top-level temperature_h & humidity lấy từ hour_0 (nếu có)
    if "hour_0_temperature" in flattened:
        flattened["temperature_h"] = flattened.get("hour_0_temperature")
    else:
        flattened["temperature_h"] = existing_data.get("temperature_h")
    if "hour_0_humidity" in flattened:
        flattened["humidity"] = flattened.get("hour_0_humidity")
    else:
        flattened["humidity"] = existing_data.get("humidity")

    # default location
    flattened["location"] = existing_data.get("location", "An Phú, Hồ Chí Minh")
    # keep next_hours internally for bias calc (but we will NOT push it)
    flattened["next_hours"] = next_hours
    # also expose when payload was fetched
    flattened["meta_fetched_at"] = datetime.now().isoformat()

    return flattened

# ========== Bias functions ==========
def update_bias_and_correct(next_hours: List[dict], observed_temp: Optional[float]) -> float:
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

# ========== Build payload for dashboard (NO banned keys) ==========
def build_dashboard_payload(merged: dict, lat: float = LAT, lon: float = LON, location_name: str = "An Phú, Hồ Chí Minh") -> dict:
    # prepare 4 hours from hour_0..hour_3 (mapping to hour_1..hour_4 in dashboard)
    hours = []
    for i in range(0, 4):
        hours.append({
            "time": merged.get(f"hour_{i}"),
            "temperature": merged.get(f"hour_{i}_temperature"),
            "humidity": merged.get(f"hour_{i}_humidity"),
            "desc": merged.get(f"hour_{i}_weather_desc")
        })

    payload = {
        "location": merged.get("location", location_name),
        "latitude": lat,
        "longitude": lon,
        "temperature_h": hours[0]["temperature"],
        "humidity": hours[0]["humidity"],
        "hour_1": hours[0]["time"],
        "hour_1_temperature": hours[0]["temperature"],
        "hour_1_humidity": hours[0]["humidity"],
        "hour_1_weather_desc": hours[0]["desc"],
        "hour_2": hours[1]["time"],
        "hour_2_temperature": hours[1]["temperature"],
        "hour_2_humidity": hours[1]["humidity"],
        "hour_2_weather_desc": hours[1]["desc"],
        "hour_3": hours[2]["time"],
        "hour_3_temperature": hours[2]["temperature"],
        "hour_3_humidity": hours[2]["humidity"],
        "hour_3_weather_desc": hours[2]["desc"],
        "hour_4": hours[3]["time"],
        "hour_4_temperature": hours[3]["temperature"],
        "hour_4_humidity": hours[3]["humidity"],
        "hour_4_weather_desc": hours[3]["desc"],
        "weather_tomorrow_min": merged.get("weather_tomorrow_min"),
        "weather_tomorrow_max": merged.get("weather_tomorrow_max"),
        "humidity_tomorrow": merged.get("humidity_tomorrow"),
    }

    # drop None
    return {k: v for k, v in payload.items() if v is not None}

# ========== Sanitize & push (drop banned keys) ==========
BANNED_KEYS = {"battery", "crop", "next_hours", "illuminance", "avg_soil_moisture"}

def sanitize_for_tb(payload: dict) -> dict:
    sanitized = {}
    for k, v in payload.items():
        if k in BANNED_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)):
            sanitized[k] = v
        else:
            try:
                sanitized[k] = json.dumps(v, ensure_ascii=False)
            except Exception:
                sanitized[k] = str(v)
    return sanitized

def send_to_thingsboard(data: dict):
    if not TB_DEVICE_URL:
        logger.warning("[TB SKIP] No TB token configured; skipping push.")
        return None
    sanitized = sanitize_for_tb(data)
    if not sanitized:
        logger.info("[TB SKIP] nothing to push after sanitization")
        return None
    logger.info(f"[TB PUSH] keys={list(sanitized.keys())}")
    try:
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] status={r.status_code}, body={r.text}")
        return r
    except Exception as e:
        logger.error(f"[TB ERROR] {e}")
        return None

# ========== FastAPI endpoints ==========
app = FastAPI(title="Agri-Bot (Open-Meteo only, ceil hours)")

class SensorData(BaseModel):
    # Endpoint để nhận sensor thật (nếu có). Không push battery/crop từ đây.
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    battery: Optional[float] = None

@app.get("/")
def root():
    return {"status": "running", "time": _now_local().isoformat(), "tb_ok": bool(TB_DEVICE_URL)}

@app.get("/weather")
def weather_endpoint():
    merged = merge_weather_and_hours(existing_data={})
    # remove next_hours for public endpoint
    merged.pop("next_hours", None)
    return merged

@app.post("/esp32-data")
def receive_data(data: SensorData):
    """
    Nhận dữ liệu thật (nếu có).
    - Cập nhật bias nếu có observed temperature (không push battery/crop)
    - Sau đó build payload forecast-only và push
    """
    logger.info(f"[RX] sensor: {data.json()}")
    merged = merge_weather_and_hours(existing_data={})
    bias = 0.0
    try:
        if data.temperature is not None:
            bias = update_bias_and_correct(merged.get("next_hours", []), data.temperature)
            insert_history_to_db(merged.get("next_hours", [{}])[0].get("temperature", 0), data.temperature, provider="sensor")
    except Exception:
        pass

    merged_with_flags = {**merged, "forecast_bias": bias, "forecast_history_len": len(bias_history)}
    payload = build_dashboard_payload(merged_with_flags)
    # ensure banned keys removed
    for k in list(BANNED_KEYS):
        payload.pop(k, None)
    send_to_thingsboard(payload)
    return {"ok": True, "forecast_bias": bias}

# ========== AUTO-LOOP ==========
async def auto_loop():
    logger.info("Auto-loop (Open-Meteo only, ceil hours) started")
    while True:
        try:
            merged = merge_weather_and_hours(existing_data={})
            merged.setdefault("forecast_bias", 0.0)
            merged.setdefault("forecast_history_len", len(bias_history))
            payload = build_dashboard_payload(merged)
            for k in list(BANNED_KEYS):
                payload.pop(k, None)
            send_to_thingsboard(payload)
        except Exception as e:
            logger.error(f"[AUTO] error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info(f"Startup: TB_DEVICE_URL present: {bool(TB_DEVICE_URL)}")
    asyncio.create_task(auto_loop())

# ========== CLI Runner ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
