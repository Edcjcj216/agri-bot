# main.py
# Agri-bot — Forecast only (Open-Meteo), push 4 next hours + avg humidity, VN time
import os
import time
import logging
import requests
import asyncio
from fastapi import FastAPI
from datetime import datetime, timedelta

# zoneinfo for timezone handling
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ============== CONFIG =================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "DEOAyARAvPbZkHKFVJQa")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 600))  # 600 giây = 10 phút
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")
EXTENDED_HOURS = 4   # chỉ lấy 4 giờ kế tiếp
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# ============== MAPPINGS =================
WEATHER_CODE_MAP = {
    0: "Nắng", 1: "Nắng nhẹ", 2: "Ít mây", 3: "Nhiều mây",
    45: "Sương muối", 48: "Sương muối",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có giông", 96: "Có giông", 99: "Có giông",
}

weather_cache = {"ts": 0, "data": {}}

# ----------------- Time helpers -----------------
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
    # Open-Meteo trả ISO có phút (YYYY-MM-DDTHH:MM)
    try:
        dt = datetime.fromisoformat(timestr)
    except Exception:
        try:
            dt = datetime.strptime(timestr, "%Y-%m-%d %H:%M")
        except Exception:
            return None
    if dt.tzinfo is None and ZoneInfo is not None:
        return dt.replace(tzinfo=ZoneInfo(TIMEZONE))
    return dt

# ================== OPEN-METEO FETCHER ==================
def fetch_open_meteo():
    base = "https://api.open-meteo.com/v1/forecast"
    daily_vars = "weathercode,temperature_2m_max,temperature_2m_min"
    hourly_vars = "temperature_2m,relativehumidity_2m,weathercode"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": daily_vars,
        "hourly": hourly_vars,
        "timezone": TIMEZONE,
        "forecast_days": 3
    }
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo request failed: {e}")
        return [], [], {}

    # parse daily
    daily_list = []
    d = data.get("daily", {})
    times = d.get("time", [])
    wc = d.get("weathercode", [])
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])

    for i in range(len(times)):
        code = wc[i] if i < len(wc) else None
        desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        daily_list.append({
            "date": times[i],
            "desc": desc,
            "max": tmax[i] if i < len(tmax) else None,
            "min": tmin[i] if i < len(tmin) else None,
        })

    # parse hourly
    hourly_list = []
    h = data.get("hourly", {})
    h_times = h.get("time", [])
    h_temp = h.get("temperature_2m", [])
    h_humi = h.get("relativehumidity_2m", [])
    h_code = h.get("weathercode", [])

    for i in range(len(h_times)):
        code = h_code[i] if i < len(h_code) else None
        short_desc = WEATHER_CODE_MAP.get(code) if code is not None else None
        hourly_list.append({
            "time": h_times[i],
            "temperature": h_temp[i] if i < len(h_temp) else None,
            "humidity": h_humi[i] if i < len(h_humi) else None,
            "weather_desc": short_desc,
        })

    return daily_list, hourly_list, data

# ================== MERGE HELPERS ==================
def merge_weather():
    daily_list, hourly_list, raw = fetch_open_meteo()
    now = _now_local()
    today_str = now.date().isoformat()
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()

    def find_daily_by_date(date):
        for d in daily_list:
            if d.get("date") == date:
                return d
        return {}

    flattened = {
        "forecast_fetched_at": now.isoformat(),
        "forecast_meta_latitude": LAT,
        "forecast_meta_longitude": LON,
    }

    # today / tomorrow summaries
    t = find_daily_by_date(today_str)
    flattened["forecast_today_desc"] = t.get("desc")
    flattened["forecast_today_max"] = t.get("max")
    flattened["forecast_today_min"] = t.get("min")

    tt = find_daily_by_date(tomorrow_str)
    flattened["forecast_tomorrow_desc"] = tt.get("desc")
    flattened["forecast_tomorrow_max"] = tt.get("max")
    flattened["forecast_tomorrow_min"] = tt.get("min")

    # === Avg humidity (today & tomorrow) ===
    def avg_humidity_for(date_iso):
        vals = []
        for h in hourly_list:
            dt = _to_local_dt(h.get("time"))
            if dt and dt.date().isoformat() == date_iso:
                hv = h.get("humidity")
                if hv is not None:
                    vals.append(float(hv))
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)

    today_avg_h = avg_humidity_for(today_str)
    tomorrow_avg_h = avg_humidity_for(tomorrow_str)
    if today_avg_h is not None:
        flattened["forecast_today_avg_humidity"] = today_avg_h
    if tomorrow_avg_h is not None:
        flattened["forecast_tomorrow_avg_humidity"] = tomorrow_avg_h

    # === Hourly forecast (4 giờ kế tiếp) ===
    parsed_times = [_to_local_dt(h.get("time")) for h in hourly_list]

    # ===== LÀM TRÒN GIỜ THEO YÊU CẦU: nếu phút >= 1 thì round up lên giờ kế tiếp =====
    now_rounded = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 1:
        now_rounded = now_rounded + timedelta(hours=1)

    start_idx = None
    for i, p in enumerate(parsed_times):
        if p and p >= now_rounded:
            start_idx = i
            break
    if start_idx is None:
        start_idx = 0

    for idx_h in range(0, EXTENDED_HOURS):  # 0..3
        i = start_idx + idx_h
        if i >= len(hourly_list):
            break
        h = hourly_list[i]
        parsed = _to_local_dt(h.get("time"))
        time_label = parsed.strftime("%H:%M") if parsed else h.get("time")

        flattened[f"forecast_hour_{idx_h}_time"] = time_label
        flattened[f"forecast_hour_{idx_h}_temp"] = h.get("temperature")
        flattened[f"forecast_hour_{idx_h}_humidity"] = h.get("humidity")
        flattened[f"forecast_hour_{idx_h}_weather"] = h.get("weather_desc")

    return flattened

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ sending payload (keys: {list(data.keys())})")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.get("/forecast")
def forecast_endpoint():
    if time.time() - weather_cache.get("ts", 0) < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]
    res = merge_weather()
    weather_cache["data"] = res
    weather_cache["ts"] = time.time()
    return res

# ================== AUTO LOOP ==================
async def auto_loop():
    logger.info("Auto-loop forecast sender started")
    while True:
        try:
            data = merge_weather()
            send_to_thingsboard(data)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_loop())
