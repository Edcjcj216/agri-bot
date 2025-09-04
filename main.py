# ============================================================
# main.py
# Agri-bot — Open-Meteo only (FULL, verbose, VN comments)
#
# Tính năng chính:
#  - Lấy dự báo từ Open-Meteo (hourly + daily).
#  - START HOUR = làm tròn lên giờ kế tiếp (vd 09:12 -> 10:00).
#  - Dashboard cần 4 giờ tới: xuất các key hour_1 .. hour_4
#    (bắt đầu từ giờ kế tiếp).
#  - temperature_h, humidity = lấy từ hour_1_*.
#  - Có weather_tomorrow_desc, min, max, và humidity_tomorrow.
#  - Chỉ push các key dashboard yêu cầu. KHÔNG push: crop, battery, next_hours.
#  - illuminance, avg_soil_moisture: nếu có sensor thì push, không có thì giữ None.
#  - Có auto-loop, logging, DB lưu bias_history tối giản (không bắt buộc, nhưng giữ theo bản dài).
# ============================================================

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

# ---------------- Timezone ----------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    LOCAL_TZ = None

# ---------------- Cấu hình chung ----------------
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "600"))   # giây giữa các lần auto-push
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))          # timeout HTTP
DB_FILE = os.getenv("DB_FILE", "agri_bot.db")                      # file DB SQLite để lưu bias
LAT = float(os.getenv("LAT", "10.762622"))                         # toạ độ mặc định (HCM)
LON = float(os.getenv("LON", "106.660172"))
EXTENDED_HOURS = 4  # Dashboard cần đúng 4 giờ tới: hour_1..hour_4

# ---------------- ThingsBoard ----------------
_raw_token = (os.getenv("TB_DEMO_TOKEN") or os.getenv("TB_TOKEN") or os.getenv("TB_DEVICE_TOKEN") or "").strip()
TB_HOST = (os.getenv("TB_HOST") or os.getenv("TB_URL") or "https://thingsboard.cloud").strip().rstrip("/")
TB_DEVICE_URL = f"{TB_HOST}/api/v1/{_raw_token}/telemetry" if _raw_token else None

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

# ============================================================
# WEATHER CODE → Tiếng Việt (bám theo mapping bạn đã dùng)
# ============================================================
WEATHER_CODE_MAP = {
    0: "Nắng",
    1: "Nắng nhẹ",
    2: "Ít mây",
    3: "Nhiều mây",
    45: "Sương muối",
    48: "Sương muối",
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
    95: "Có giông",
    96: "Có giông",
    99: "Có giông",
}

# ============================================================
# DB: lưu một ít lịch sử để tính bias nhiệt độ (tuỳ chọn)
# ============================================================
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL,          -- nhiệt độ dự báo tại giờ gần nhất
                observed_temp REAL,     -- nhiệt độ đo thực tế (nếu có)
                ts INTEGER,             -- timestamp
                provider TEXT           -- 'open-meteo' | 'sensor'
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

def insert_history_to_db(api_temp: Optional[float], observed_temp: Optional[float], provider="open-meteo"):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts, provider) VALUES (?, ?, ?, ?)",
            (None if api_temp is None else float(api_temp),
             None if observed_temp is None else float(observed_temp),
             int(time.time()),
             provider)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"insert_history_to_db error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# Giữ tối đa N mẫu gần nhất trong RAM
bias_history: deque[tuple[Optional[float], Optional[float]]] = deque(maxlen=int(os.getenv("BIAS_MAX_HISTORY", "48")))

# ============================================================
# Tiện ích thời gian
# ============================================================
def _now_local() -> datetime:
    """Trả về thời gian hiện tại theo timezone VN (nếu có ZoneInfo)."""
    try:
        return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()
    except Exception:
        return datetime.now()

def _to_local_dt(timestr: Optional[str]) -> Optional[datetime]:
    """Parse ISO/naive và gán tz local nếu thiếu tzinfo."""
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
    """
    Nếu dt đang chưa đến đầu giờ (có phút/giây) -> làm tròn lên đầu giờ kế tiếp.
    Nếu đang đúng hh:00:00 -> giữ nguyên.
    """
    if dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    return (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))

# ============================================================
# Open-Meteo fetcher
# ============================================================
def fetch_open_meteo() -> tuple[list[dict], list[dict], dict]:
    """
    Trả về (daily_list, hourly_list, raw_json)
      - daily_list: [{date, desc, max, min, precipitation_sum}]
      - hourly_list: [{time, temperature, humidity, weather_code, weather_*,
                       precipitation, precipitation_probability, windspeed, winddir}]
    """
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
        "past_days": 1,       # để tính humidity_today (trong 24h gần nhất)
        "forecast_days": 3    # hôm nay + mai + mốt
    }

    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Open-Meteo fetch error: {e}")
        return [], [], {}

    # ----- Parse daily -----
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

    # ----- Parse hourly -----
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
# Merge dữ liệu & chọn 4 giờ tới (giờ kế tiếp là hour_1)
# ============================================================
def merge_weather_and_hours(existing: Optional[dict] = None) -> dict:
    """
    - Xác định start_time = ceil_to_next_hour(now_local).
    - Từ danh sách hourly, lấy 4 điểm tại/ sau start_time:
        hour_1 (start_time), hour_2 (+1h), hour_3 (+2h), hour_4 (+3h).
    - Top-level temperature_h, humidity = từ hour_1_*.
    - Bổ sung weather_tomorrow_* và humidity_tomorrow (nếu đủ dữ liệu).
    """
    existing = existing or {}
    daily_list, hourly_list, raw = fetch_open_meteo()

    now = _now_local()
    start_time = ceil_to_next_hour(now)

    # ----- Daily: today / tomorrow -----
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
        merged["weather_tomorrow_desc"] = tomorrow.get("desc")  # BẮT BUỘC có
        merged["weather_tomorrow_max"] = tomorrow.get("max")
        merged["weather_tomorrow_min"] = tomorrow.get("min")

    # ----- Hourly: tìm index >= start_time -----
    parsed_times: List[Optional[datetime]] = []
    for h in hourly_list:
        parsed_times.append(_to_local_dt(h.get("time")))

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
        start_idx = 0  # hiếm khi xảy ra, nhưng cứ lấy đầu danh sách

    # ----- Lấy đúng 4 giờ: hour_1..hour_4 -----
    selected: list[dict] = []
    for offset in range(EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        selected.append(hourly_list[i])

    # map vào merged theo format dashboard
    for k, item in enumerate(selected, start=1):  # k: 1..4
        dt_local = _to_local_dt(item.get("time"))
        label = dt_local.strftime("%H:%M") if dt_local else item.get("time")
        merged[f"hour_{k}"] = label
        if item.get("temperature") is not None:
            merged[f"hour_{k}_temperature"] = item.get("temperature")
        if item.get("humidity") is not None:
            merged[f"hour_{k}_humidity"] = item.get("humidity")
        wlabel = item.get("weather_short") or item.get("weather_desc")
        merged[f"hour_{k}_weather_desc"] = wlabel

    # ----- Top-level hiển thị: từ hour_1 -----
    merged["temperature_h"] = merged.get("hour_1_temperature")
    merged["humidity"] = merged.get("hour_1_humidity")

    # ----- Humidity trung bình hôm nay / ngày mai (nếu đủ 48 điểm) -----
    hums = [h.get("humidity") for h in hourly_list if isinstance(h.get("humidity"), (int, float))]
    if len(hums) >= 24:
        merged["humidity_today"] = round(sum(hums[:24]) / 24.0, 1)
    if len(hums) >= 48:
        merged["humidity_tomorrow"] = round(sum(hums[24:48]) / 24.0, 1)

    # ----- Meta + location -----
    merged["location"] = existing.get("location", "An Phú, Hồ Chí Minh")
    merged["latitude"] = LAT
    merged["longitude"] = LON
    merged["meta_fetched_at"] = _now_local().isoformat()

    return merged

# ============================================================
# Bias (tùy chọn): cập nhật chênh lệch nếu có nhiệt độ thực tế
# ============================================================
def update_bias_and_correct(selected_first: Optional[dict], observed_temp: Optional[float]) -> float:
    """
    Lấy nhiệt độ dự báo tại hour_1 (điểm đầu của 'selected') so với observed_temp.
    Lưu vào deque + DB để tính bias trung bình = mean(observed - api).
    """
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
# Build payload đẩy ThingsBoard (đúng schema dashboard)
# ============================================================
# Lưu giá trị cảm biến gần nhất (illum, soil_moisture) để auto-loop có thể dùng
LATEST_SENSOR: dict[str, Optional[float]] = {
    "illuminance": None,
    "avg_soil_moisture": None,
}

def build_dashboard_payload(merged: dict) -> dict:
    """
    Xuất đúng các key mà dashboard sẽ bind:
      - location, latitude, longitude
      - temperature_h, humidity (từ hour_1)
      - hour_1..hour_4 (time), kèm *_temperature, *_humidity, *_weather_desc
      - weather_tomorrow_min, weather_tomorrow_max, weather_tomorrow_desc
      - humidity_tomorrow
      - illuminance, avg_soil_moisture (có sensor thì push, không thì None)
    """
    payload = {
        "location": merged.get("location"),
        "latitude": merged.get("latitude"),
        "longitude": merged.get("longitude"),

        # Top-level hiển thị
        "temperature_h": merged.get("hour_1_temperature"),
        "humidity": merged.get("hour_1_humidity"),

        # 4 giờ tới
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

        # Ngày mai
        "weather_tomorrow_min": merged.get("weather_tomorrow_min"),
        "weather_tomorrow_max": merged.get("weather_tomorrow_max"),
        "weather_tomorrow_desc": merged.get("weather_tomorrow_desc"),
        "humidity_tomorrow": merged.get("humidity_tomorrow"),

        # Sensor (nếu không có, để None -> TB sẽ lưu null)
        "illuminance": LATEST_SENSOR.get("illuminance"),
        "avg_soil_moisture": LATEST_SENSOR.get("avg_soil_moisture"),
    }

    # Loại bỏ key có giá trị None? => KHÔNG. Bạn yêu cầu: không có sensor thì để null.
    # Tuy nhiên TB chấp nhận null. Nếu cần loại None thì bỏ comment dưới:
    # payload = {k: v for k, v in payload.items() if v is not None}

    return payload

# ---------------- Các key bị cấm đẩy lên TB ----------------
BANNED_KEYS = {
    "battery",       # không push
    "crop",          # không push
    "next_hours",    # không push
}

def sanitize_for_tb(payload: dict) -> dict:
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        if k in BANNED_KEYS:
            continue
        # Cho phép None (null) với illuminance/avg_soil_moisture theo yêu cầu
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
# FastAPI
# ============================================================
app = FastAPI(title="Agri-Bot (Open-Meteo only, hour_1 display, VN)")

class SensorData(BaseModel):
    """
    Endpoint nhận dữ liệu cảm biến thật (nếu có).
    - temperature / humidity: dùng để tính bias (optional).
    - illuminance / avg_soil_moisture: lưu lại và push ra dashboard (nếu có).
    - battery: nhận nhưng KHÔNG push (bị cấm).
    """
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    illuminance: Optional[float] = None
    avg_soil_moisture: Optional[float] = None
    battery: Optional[float] = None  # sẽ bỏ qua khi push

@app.get("/")
def root():
    return {
        "status": "running",
        "time": _now_local().isoformat(),
        "tb_ok": bool(TB_DEVICE_URL),
        "lat": LAT,
        "lon": LON,
    }

@app.get("/weather")
def weather_endpoint():
    """
    Trả về gói merged cho kiểm tra nhanh (KHÔNG có next_hours).
    """
    merged = merge_weather_and_hours({})
    # Bảo đảm không lòi key thừa
    merged.pop("next_hours", None)
    return merged

@app.post("/esp32-data")
def receive_data(data: SensorData):
    """
    Nhận sensor thật. Hành vi:
    - Cập nhật bộ nhớ LATEST_SENSOR (illum/soil).
    - Nếu có temperature thực tế: cập nhật bias_history.
    - Sau đó build payload forecast-only (hour_1..hour_4) + sensor (illum/soil) -> push TB.
    - KHÔNG push battery/crop/next_hours.
    """
    logger.info(f"[RX SENSOR] {data.json()}")

    # Cập nhật 2 sensor có ích cho dashboard
    if data.illuminance is not None:
        LATEST_SENSOR["illuminance"] = float(data.illuminance)
    if data.avg_soil_moisture is not None:
        LATEST_SENSOR["avg_soil_moisture"] = float(data.avg_soil_moisture)

    # Lấy forecast và tính bias nếu có observed temperature
    merged = merge_weather_and_hours({})
    # "first selected hour" = hour_1 => lấy lại từ merged
    selected_first = {
        "temperature": merged.get("hour_1_temperature"),
        "humidity": merged.get("hour_1_humidity"),
    }
    bias = 0.0
    try:
        if data.temperature is not None:
            bias = update_bias_and_correct(selected_first, float(data.temperature))
    except Exception:
        pass

    # Có thể lưu meta bias (không push thêm key lạ)
    merged["forecast_bias"] = bias
    merged["forecast_history_len"] = len(bias_history)

    payload = build_dashboard_payload(merged)
    # đảm bảo banned keys không xuất hiện
    for k in list(BANNED_KEYS):
        payload.pop(k, None)
    send_to_thingsboard(payload)

    return {"ok": True, "bias": bias, "saved_illum": LATEST_SENSOR["illuminance"], "saved_soil": LATEST_SENSOR["avg_soil_moisture"]}

# ============================================================
# AUTO-LOOP: định kỳ pull Open-Meteo & push TB
# ============================================================
async def auto_loop():
    logger.info("Auto-loop started (Open-Meteo only, hour_1 as display)")
    while True:
        try:
            merged = merge_weather_and_hours({})
            # không thêm key lạ; bias không có sensor thì giữ 0
            merged.setdefault("forecast_bias", 0.0)
            merged.setdefault("forecast_history_len", len(bias_history))

            payload = build_dashboard_payload(merged)
            for k in list(BANNED_KEYS):
                payload.pop(k, None)
            send_to_thingsboard(payload)
        except Exception as e:
            logger.error(f"[AUTO] {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info(f"Startup: TB_DEVICE_URL present: {bool(TB_DEVICE_URL)}")
    # Chạy auto-loop nền
    asyncio.create_task(auto_loop())

# ============================================================
# CLI runner (chạy cục bộ)
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        log_level="info",
    )
