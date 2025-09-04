# main.py
# Agri-bot — Open-Meteo only, chuẩn, có chú thích tiếng Việt
# - Lấy forecast từ Open-Meteo
# - Build payload khớp dashboard (hour_1..hour_4, temperature_h, humidity, weather_tomorrow_*)
# - Không gửi battery/crop/next_hours/illuminance/avg_soil_moisture (những sensor khác chịu trách nhiệm)
# - Nếu TB token rỗng sẽ SKIP push (tránh 400 Bad Request)
# - Auto-loop lấy Open-Meteo và push theo AUTO_LOOP_INTERVAL

import os
import time
import json
import logging
import sqlite3
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any

import requests
from fastapi import FastAPI
from pydantic import BaseModel

# zoneinfo optional (Python 3.9+)
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    LOCAL_TZ = None

# ==================== CẤU HÌNH ====================
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "600"))   # 600s = 10 phút
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
DB_FILE = os.getenv("DB_FILE", "agri_bot.db")
LAT = float(os.getenv("LAT", "10.762622"))     # default: HCM center
LON = float(os.getenv("LON", "106.660172"))
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", "4"))  # dashboard cần 4 giờ tới

# ThingsBoard token/host: chấp nhận nhiều tên env var
_raw_token = (os.getenv("TB_DEMO_TOKEN") or os.getenv("TB_TOKEN") or os.getenv("TB_DEVICE_TOKEN") or "").strip()
TB_HOST = (os.getenv("TB_HOST") or os.getenv("TB_URL") or "https://thingsboard.cloud").strip().rstrip("/")
TB_DEVICE_URL = f"{TB_HOST}/api/v1/{_raw_token}/telemetry" if _raw_token else None

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agri-bot")

def _mask_token(t: str) -> str:
    if not t:
        return "<empty>"
    t = t.strip()
    if len(t) <= 8:
        return t[:2] + "***"
    return t[:4] + "..." + t[-4:]

logger.info(f"[ENV] TB_HOST={TB_HOST}")
logger.info(f"[ENV] TB_TOKEN={_mask_token(_raw_token)} (len={len(_raw_token)})")
logger.info(f"[ENV] TB_DEVICE_URL present = {bool(TB_DEVICE_URL)}")
if TB_DEVICE_URL:
    logger.info(f"[ENV] TB_DEVICE_URL={TB_DEVICE_URL}")

# ==================== MAPPING THỜI TIẾT (VN) ====================
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

# ==================== DATABASE NHỎ (bias history) ====================
def init_db() -> None:
    """Khởi tạo DB SQLite dùng cho lưu lịch sử bias (nếu có)"""
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

def insert_history_to_db(api_temp: float, observed_temp: float, provider: str = "open-meteo") -> None:
    """Ghi cặp (api_temp, observed_temp) vào DB"""
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

# in-memory bias history (dùng để tính forecast_bias tạm)
bias_history = deque(maxlen=int(os.getenv("BIAS_MAX_HISTORY", "48")))

# ==================== TIỆN ÍCH THỜI GIAN ====================
def _now_local() -> datetime:
    if LOCAL_TZ:
        try:
            return datetime.now(LOCAL_TZ)
        except Exception:
            return datetime.now()
    return datetime.now()

def _to_local_dt(timestr: Optional[str]) -> Optional[datetime]:
    """Parse ISO-like time string và attach tz nếu cần"""
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

# ==================== FETCH OPEN-METEO ====================
def fetch_open_meteo() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool, dict]:
    """
    Gọi Open-Meteo, parse thành:
    - daily_list: list dict {date, desc, max, min, precipitation_sum}
    - hourly_list: list dict {time, temperature, humidity, weather_code, weather_short, ...}
    - has_yday (bool)
    - raw json
    """
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
            "precipitation_sum": psum[i] if i < len(psum) else None,
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

    has_yday = True
    return daily_list, hourly_list, has_yday, data

# ==================== MERGE -> flattened telemetry ====================
def merge_weather_and_hours(existing_data: Optional[dict] = None) -> dict:
    """
    Trả về flattened dict chứa các key giống template dashboard.
    - existing_data có thể chứa override (ví dụ location)
    - tạo hour_0 .. hour_{EXTENDED_HOURS-1} và các trường daily
    """
    if existing_data is None:
        existing_data = {}
    daily_list, hourly_list, has_yday, raw = fetch_open_meteo()
    now = _now_local()
    flattened: dict = {**existing_data}

    # map daily today & tomorrow
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

    # parse hourly times & pick start index robust (tương tự code trước)
    parsed_times: List[Optional[datetime]] = []
    hour_times = [h.get("time") for h in hourly_list] if hourly_list else []
    for t in hour_times:
        parsed_times.append(_to_local_dt(t))

    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    start_idx = 0
    try:
        for i, p in enumerate(parsed_times):
            if p is None:
                continue
            p_comp = p
            now_comp = now_rounded
            if p_comp.tzinfo is None and now_comp.tzinfo is not None:
                p_comp = p_comp.replace(tzinfo=now_comp.tzinfo)
            if now_comp.tzinfo is None and p_comp.tzinfo is not None:
                now_comp = now_comp.replace(tzinfo=p_comp.tzinfo)
            if p_comp >= now_comp:
                start_idx = i
                break
    except Exception as e:
        logger.warning(f"select index error: {e}")
        start_idx = 0

    # build next_hours (internal)
    next_hours: List[dict] = []
    for offset in range(EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        next_hours.append(hourly_list[i])

    # tạo các key hour_0 .. hour_{EXTENDED_HOURS-1}
    for idx_h, h in enumerate(next_hours):
        parsed = _to_local_dt(h.get("time"))
        time_label = parsed.strftime("%H:%M") if parsed is not None else h.get("time")
        flattened[f"hour_{idx_h}"] = time_label
        flattened[f"hour_{idx_h}_time_local"] = parsed.isoformat() if parsed is not None else h.get("time")
        if h.get("temperature") is not None:
            flattened[f"hour_{idx_h}_temperature"] = h.get("temperature")
        if h.get("humidity") is not None:
            flattened[f"hour_{idx_h}_humidity"] = h.get("humidity")
        # weather desc prefer mapping sẵn
        short_label = h.get("weather_short") or (WEATHER_CODE_MAP.get(int(h.get("weather_code"))) if h.get("weather_code") is not None else None)
        if not short_label:
            short_label = h.get("weather_desc")
        flattened[f"hour_{idx_h}_weather_short"] = short_label
        flattened[f"hour_{idx_h}_weather_desc"] = short_label

    # aggregated humidity (best-effort)
    hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
    if hums:
        flattened["humidity_today"] = round(sum(hums[:24]) / min(24, len(hums)), 1)
    if len(hums) >= 48:
        flattened["humidity_tomorrow"] = round(sum(hums[24:48]) / 24.0, 1)

    # top-level temperature_h / humidity lấy từ hour_0 nếu có
    if "hour_0_temperature" in flattened:
        flattened["temperature_h"] = flattened.get("hour_0_temperature")
    else:
        flattened["temperature_h"] = existing_data.get("temperature_h")
    if "hour_0_humidity" in flattened:
        flattened["humidity"] = flattened.get("hour_0_humidity")
    else:
        flattened["humidity"] = existing_data.get("humidity")

    # location mặc định
    flattened["location"] = existing_data.get("location", "An Phú, Hồ Chí Minh")

    # expose next_hours nội bộ (không push trực tiếp)
    flattened["next_hours"] = next_hours

    return flattened

# ==================== BIAS (nếu có sensor gửi) ====================
def update_bias_and_correct(next_hours: List[dict], observed_temp: Optional[float]) -> float:
    """Cập nhật bias history (api vs observed) và trả về bias (đơn vị °C)"""
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

# ==================== BUILD PAYLOAD CHO DASHBOARD ====================
def build_dashboard_payload(merged: dict, lat: float = LAT, lon: float = LON, location_name: str = "An Phú, Hồ Chí Minh") -> dict:
    """
    Sinh payload đúng key dashboard:
    - top-level: location, latitude, longitude, temperature_h, humidity
    - hour_1..hour_4, hour_X_temperature, hour_X_humidity, hour_X_weather_desc
    - weather_tomorrow_min/max, humidity_tomorrow
    - illuminance & avg_soil_moisture: **KHÔNG** bật ở đây (device khác push)
    """
    # lấy 4 giờ (mapped từ hour_0..hour_3 trong merged)
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
        # top-level hiển thị lấy từ hour_0
        "temperature_h": hours[0]["temperature"],
        "humidity": hours[0]["humidity"],
        # forecast (dashboard expects hour_1..hour_4)
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
        # ngày mai
        "weather_tomorrow_min": merged.get("weather_tomorrow_min"),
        "weather_tomorrow_max": merged.get("weather_tomorrow_max"),
        "humidity_tomorrow": merged.get("humidity_tomorrow"),
    }

    # chuẩn hóa: nếu giá trị None thì omit (ThingsBoard nhận key ít hơn thay vì null)
    out = {}
    for k, v in payload.items():
        if v is None:
            continue
        out[k] = v
    return out

# ==================== SANITIZE & PUSH THINGSBOARD ====================
def sanitize_for_tb(payload: dict) -> dict:
    """Chuyển các giá trị phức tạp thành string JSON; loại bỏ các key không cần gửi"""
    to_drop = {"battery", "crop", "next_hours", "illuminance", "avg_soil_moisture"}
    sanitized = {}
    for k, v in payload.items():
        if k in to_drop:
            continue
        if isinstance(v, (str, int, float, bool)):
            sanitized[k] = v
        else:
            try:
                sanitized[k] = json.dumps(v, ensure_ascii=False)
            except Exception:
                sanitized[k] = str(v)
    return sanitized

def send_to_thingsboard(data: dict) -> Optional[requests.Response]:
    """Push telemetry đến ThingsBoard; nếu TB_DEVICE_URL rỗng thì skip"""
    if not TB_DEVICE_URL:
        logger.warning("[TB SKIP] No TB device token configured; skipping push.")
        return None
    try:
        sanitized = sanitize_for_tb(data)
        if not sanitized:
            logger.info("[TB SKIP] No keys to push after sanitization.")
            return None
        logger.info(f"[TB PUSH] keys={list(sanitized.keys())}")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] status={r.status_code}, body={r.text}")
        return r
    except Exception as e:
        logger.error(f"[TB ERROR] {e}")
        return None

# ==================== FASTAPI ENDPOINTS ====================
app = FastAPI(title="Agri-Bot (Open-Meteo Only)")

class SensorData(BaseModel):
    """
    Endpoint /esp32-data chỉ để nhận sensor thật nếu có.
    illuminance/avg_soil_moisture sẽ được gửi bởi thiết bị khác (không phải auto-loop này).
    """
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    battery: Optional[float] = None

@app.get("/")
def root():
    return {"status": "running", "time": _now_local().isoformat(), "tb_ok": bool(TB_DEVICE_URL)}

@app.get("/weather")
def weather_endpoint():
    """Trả về flattened merged forecast (không show next_hours để gọn)"""
    merged = merge_weather_and_hours(existing_data={})
    # xóa next_hours trước khi trả về public API
    merged.pop("next_hours", None)
    return merged

@app.post("/esp32-data")
def receive_data(data: SensorData):
    """
    Nhận dữ liệu cảm biến thật (nếu có).
    - Dùng observed temperature để update bias (nếu bạn muốn)
    - Sau đó build payload và push (payload ở đây vẫn là forecast-focused)
    """
    logger.info(f"[RX] Sensor data: {data.json()}")
    merged = merge_weather_and_hours(existing_data={})
    # cập nhật bias nếu có temperature thật
    bias = 0.0
    try:
        if data.temperature is not None:
            bias = update_bias_and_correct(merged.get("next_hours", []), data.temperature)
            # lưu lịch sử (có thể dùng để debug)
            insert_history_to_db(merged.get("next_hours", [{}])[0].get("temperature", 0), data.temperature, provider="sensor")
    except Exception:
        pass

    # build payload cho dashboard (không bao gồm illuminance/soil - device khác lo)
    merged_with_flags = {**merged, "forecast_bias": bias, "forecast_history_len": len(bias_history)}
    payload = build_dashboard_payload(merged_with_flags)
    send_to_thingsboard(payload)
    return {"ok": True, "forecast_bias": bias}

# ==================== AUTO-LOOP: chỉ fetch Open-Meteo và push forecast-only ====================
async def auto_loop():
    logger.info("Auto-loop (Open-Meteo only) started")
    while True:
        try:
            merged = merge_weather_and_hours(existing_data={})
            merged.setdefault("forecast_bias", 0.0)
            merged.setdefault("forecast_history_len", len(bias_history))
            payload = build_dashboard_payload(merged)
            send_to_thingsboard(payload)
        except Exception as e:
            logger.error(f"[AUTO] error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info(f"Startup: TB_DEVICE_URL present: {bool(TB_DEVICE_URL)}")
    # start background auto-loop
    import asyncio as _asyncio
    _asyncio.create_task(auto_loop())

# ==================== RUN (dev) ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
