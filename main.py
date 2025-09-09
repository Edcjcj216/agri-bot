# ============================================================
# main.py
# Agri-bot — Open-Meteo primary, with OWM + OpenRouter fallback
# Full, verbose, Vietnamese comments preserved (DB, bias, sensor, auto-loop)
#
# Tính năng chính (giữ nguyên bản dài):
#  - Lấy dự báo từ Open-Meteo (hourly + daily) — nguồn chính.
#  - Nếu Open-Meteo không trả dữ liệu => fallback OpenWeatherMap (OWM).
#  - Nếu OWM không trả dữ liệu => fallback OpenRouter (nếu config API key).
#  - START HOUR = làm tròn lên giờ kế tiếp (vd 09:12 -> 10:00). Nếu đúng đầu giờ thì giữ.
#  - Dashboard cần 4 giờ tới: xuất các key hour_1 .. hour_4 (bắt đầu từ giờ kế tiếp).
#  - temperature_h, humidity = lấy từ hour_1_*.
#  - Có weather_tomorrow_desc, min, max, và humidity_tomorrow.
#  - Chỉ push các key dashboard yêu cầu. KHÔNG push: crop, battery, next_hours.
#  - illuminance, avg_soil_moisture: nếu có sensor thì push, không có thì giữ None.
#  - Có auto-loop, logging, DB lưu bias_history tối giản.
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

# ---------------- Fallback keys ----------------
OWM_API_KEY = os.getenv("OWM_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

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

# ============================================================
# WEATHER CODE → Tiếng Việt
# ============================================================
WEATHER_CODE_MAP = {
    0: "Trời nắng đẹp",
    1: "Trời không mây",
    2: "Trời có mây",
    3: "Trời nhiều mây",
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
# Open-Meteo fetcher (không đổi)
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
        "forecast_days": 3
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
# OWM fetcher + mapper -> chuẩn hoá giống Open-Meteo parsed lists
# ============================================================
def fetch_owm_and_map() -> tuple[list[dict], list[dict], dict]:
    """
    Gọi OWM 5-day/3-hour forecast (endpoint forecast) và map về cấu trúc daily_list, hourly_list
    Trả về daily_list, hourly_list, raw_json
    """
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

    # hourly_list: OWM provides 3-hour steps — map each item
    hourly_list: list[dict] = []
    for item in data.get("list", []):
        # item.dt is unix
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
        hourly_list.append({
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
        })

    # daily_list: OWM returns city/timezone + we can aggregate per day (min/max)
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
        # choose most common desc
        descs = [x for x in v.get("desc_list") if x]
        desc = max(set(descs), key=descs.count) if descs else None
        daily_list.append({"date": d, "desc": desc, "max": v.get("max"), "min": v.get("min"), "precipitation_sum": None})

    return daily_list, hourly_list, data

# ============================================================
# OpenRouter fetcher + mapper (best-effort)
# ============================================================
def fetch_openrouter_and_map() -> tuple[list[dict], list[dict], dict]:
    if not OPENROUTER_API_KEY:
        return [], [], {}
    # NOTE: OpenRouter weather endpoints may vary; this is a placeholder best-effort
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

    # Try to find hourly list in common fields
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
        hourly_list.append({
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
        })

    # daily: attempt to compute min/max
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
# Merge dữ liệu & chọn 4 giờ tới (giờ kế tiếp là hour_1)
# - Bây giờ có fallback: Open-Meteo -> OWM -> OpenRouter
# ============================================================
def merge_weather_and_hours(existing: Optional[dict] = None) -> dict:
    existing = existing or {}

    # 1) Try Open-Meteo
    daily_list, hourly_list, raw = fetch_open_meteo()
    source = "open-meteo" if hourly_list else None

    # 2) fallback OWM if no hourly data
    if not hourly_list:
        d_owm, h_owm, raw_owm = fetch_owm_and_map()
        if h_owm:
            logger.info("Fallback to OWM data")
            daily_list, hourly_list, raw = d_owm, h_owm, raw_owm
            source = "owm"

    # 3) fallback OpenRouter if still empty
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
        merged["weather_tomorrow_desc"] = tomorrow.get("desc")
        merged["weather_tomorrow_max"] = tomorrow.get("max")
        merged["weather_tomorrow_min"] = tomorrow.get("min")

    # ----- Hourly: tìm index >= start_time (robust tz handling) -----
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
        start_idx = 0

    # ----- Lấy đúng 4 giờ: hour_1..hour_4 -----
    selected: list[dict] = []
    for offset in range(EXTENDED_HOURS):
        i = start_idx + offset
        if i >= len(hourly_list):
            break
        selected.append(hourly_list[i])

    # map vào merged theo format dashboard (CHỈ các key cần thiết)
    for k, item in enumerate(selected, start=1):  # k: 1..4
        dt_local = _to_local_dt(item.get("time"))
        label = dt_local.strftime("%H:%M") if dt_local else item.get("time")
        # giữ key hour_1..hour_4 (time string)
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

    # ----- Humidity trung bình hôm nay / ngày mai (nếu đủ số điểm) -----
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
    merged["meta_provider"] = source

    logger.info(f"merge done. provider={source}, start_time={start_time.isoformat()}, hour_keys={[f'hour_{i}' for i in range(1, len(selected)+1)]}")

    return merged

# ============================================================
# Bias (tùy chọn): cập nhật chênh lệch nếu có nhiệt độ thực tế
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
# Build payload đẩy ThingsBoard (đúng schema dashboard)
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

    return payload

# ---------------- Các key bị cấm đẩy lên TB ----------------
BANNED_KEYS = {
    "battery",
    "crop",
    "next_hours",
}

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
# FastAPI
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
# AUTO-LOOP: định kỳ pull Open-Meteo & push TB
# (với fallback chain đã tích hợp trong merge_weather_and_hours)
# ============================================================
async def auto_loop():
    logger.info("Auto-loop started (Open-Meteo primary, fallback OWM/OpenRouter)")
    while True:
        try:
            merged = merge_weather_and_hours({})
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
