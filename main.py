# main.py — Agri-bot Full
# ========================================
# ✅ Tính năng:
#  - Lấy dự báo thời tiết (Open-Meteo → OWM → OpenRouter fallback).
#  - Chọn block 4 giờ kế tiếp, làm tròn giờ hiện tại +1 phút.
#  - Auto-loop: cứ mỗi AUTO_LOOP_INTERVAL giây tự fetch & push ThingsBoard.
#  - Lưu/truy xuất bias nhiệt độ vào SQLite.
#  - Xuất FastAPI với endpoint /weather, /bias, /esp32-data để test.

import os
import time
import json
import logging
import requests
import asyncio
import sqlite3
import math
import random
import re
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque

# Xử lý timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ============== CONFIG =================
TB_HOST = os.getenv("TB_HOST", "https://thingsboard.cloud")
TB_TOKEN = os.getenv("TB_TOKEN", "DEOAyARAvPbZkHKFVJQa")
TB_DEVICE_URL = os.getenv("TB_DEVICE_URL", f"{TB_HOST}/api/v1/{TB_TOKEN}/telemetry")

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")

AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 600))  # 10 phút
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 900))  # 15 phút
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))

OWM_API_KEY = os.getenv("OWM_API_KEY", None)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", None)

# bias history
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")
bias_history = deque(maxlen=MAX_HISTORY)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agribot")

app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ============== WEATHER CODE MAP ==============
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

# ============== DB Helpers ==============
def init_db():
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL NOT NULL,
                observed_temp REAL NOT NULL,
                ts INTEGER NOT NULL,
                provider TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"DB init error: {e}")
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
        logger.info(f"Loaded {len(rows)} bias samples")
    except Exception as e:
        logger.warning(f"DB load error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ============== Time Utils ==============
def _now_local():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()

def _to_local_dt(timestr):
    try:
        dt = datetime.fromisoformat(timestr)
    except Exception:
        try:
            dt = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None
    if dt and dt.tzinfo is None and ZoneInfo:
        dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
    return dt

# ============== Fetch Weather ==============
def fetch_open_meteo():
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "temperature_2m,relativehumidity_2m,weathercode",
        "timezone": TIMEZONE,
    }
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json(), "open-meteo"
    except Exception as e:
        logger.warning(f"Open-Meteo fail: {e}")
        return None, None

def fetch_owm():
    if not OWM_API_KEY:
        return None, None
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json(), "owm"
    except Exception as e:
        logger.warning(f"OWM fail: {e}")
        return None, None

def fetch_openrouter():
    if not OPENROUTER_API_KEY:
        return None, None
    try:
        # Demo: trả về None (chưa kết nối OpenRouter thực)
        return None, None
    except Exception as e:
        logger.warning(f"OpenRouter fail: {e}")
        return None, None

# ============== Weather Merge ==============
def get_next_hours():
    # Lấy dữ liệu thời tiết với fallback
    data, source = fetch_open_meteo()
    if data is None:
        data, source = fetch_owm()
    if data is None:
        data, source = fetch_openrouter()
    if data is None:
        logger.error("Không fetch được thời tiết từ bất kỳ nguồn nào")
        return []

    now = _now_local()
    now_rounded = (now + timedelta(minutes=1)).replace(minute=0, second=0, microsecond=0)
    logger.info(f"Giờ hiện tại: {now.isoformat()}, chọn block từ: {now_rounded.isoformat()}")

    hours = []
    if source == "open-meteo":
        h_times = data.get("hourly", {}).get("time", [])
        h_temp = data.get("hourly", {}).get("temperature_2m", [])
        h_humi = data.get("hourly", {}).get("relativehumidity_2m", [])
        h_code = data.get("hourly", {}).get("weathercode", [])
        for i in range(len(h_times)):
            t = _to_local_dt(h_times[i])
            if not t:
                continue
            if t >= now_rounded:
                for j in range(4):
                    if i + j < len(h_times):
                        hours.append({
                            "time": _to_local_dt(h_times[i+j]).strftime("%H:%M"),
                            "temperature": h_temp[i+j],
                            "humidity": h_humi[i+j],
                            "weather": WEATHER_CODE_MAP.get(h_code[i+j], str(h_code[i+j]))
                        })
                break

    elif source == "owm":
        for item in data.get("list", []):
            t = datetime.fromtimestamp(item["dt"])
            if ZoneInfo:
                t = t.replace(tzinfo=ZoneInfo(TIMEZONE))
            if t >= now_rounded:
                for j in range(4):
                    if j < len(data["list"]):
                        sub = data["list"][j]
                        hours.append({
                            "time": datetime.fromtimestamp(sub["dt"]).strftime("%H:%M"),
                            "temperature": sub["main"]["temp"],
                            "humidity": sub["main"]["humidity"],
                            "weather": sub["weather"][0]["description"]
                        })
                break

    logger.info(f"Giờ đã chọn: {hours}")
    return hours

# ============== ThingsBoard Push ==============
def send_to_thingsboard(payload: dict):
    try:
        logger.info(f"TB ▶ {json.dumps(payload, ensure_ascii=False)}")
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=REQUEST_TIMEOUT)
        logger.info(f"TB ◀ {r.status_code} {r.text}")
    except Exception as e:
        logger.error(f"TB push error: {e}")

# ============== Bias Correction ==============
def update_bias(api_temp, observed_temp, provider="open-meteo"):
    try:
        bias_history.append((api_temp, observed_temp))
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT INTO bias_history (api_temp, observed_temp, ts, provider) VALUES (?,?,?,?)",
                    (api_temp, observed_temp, int(time.time()), provider))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Bias update fail: {e}")

# ============== AUTO LOOP ==============
async def auto_loop():
    logger.info("Auto loop bắt đầu...")
    while True:
        try:
            hours = get_next_hours()
            if hours:
                payload = {
                    "timestamp": _now_local().isoformat(),
                    "hour_1": hours[0],
                    "hour_2": hours[1] if len(hours) > 1 else None,
                    "hour_3": hours[2] if len(hours) > 2 else None,
                    "hour_4": hours[3] if len(hours) > 3 else None,
                }
                send_to_thingsboard(payload)
        except Exception as e:
            logger.error(f"Auto loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

# ============== API ROUTES ==============
@app.get("/")
def root():
    return {"status": "running", "tb_url": TB_DEVICE_URL, "lat": LAT, "lon": LON}

@app.get("/weather")
def weather():
    return {"hours": get_next_hours()}

@app.get("/bias")
def bias():
    diffs = [obs - api for api, obs in bias_history if api and obs]
    return {"bias": round(sum(diffs)/len(diffs), 2) if diffs else 0.0, "len": len(diffs)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    hours = get_next_hours()
    if hours and data.temperature:
        update_bias(hours[0]["temperature"], data.temperature)
    payload = {**data.dict(), "hours": hours}
    send_to_thingsboard(payload)
    return {"pushed": payload}

# ============== STARTUP ==============
@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())