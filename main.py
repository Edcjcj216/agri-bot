# main.py
# Agri-bot — FastAPI + Open-Meteo + ThingsBoard push

import os
import time
import json
import logging
import requests
import asyncio
import sqlite3
import random
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ================== Logging ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("agri-bot")

# ================== Config ==================
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DB_FILE = "sensor.db"
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"
REQUEST_TIMEOUT = 10

# ================== Weather mapping ==================
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

# ================== Database ==================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sensor_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        temperature REAL,
        humidity REAL,
        battery REAL
    )
    """)
    conn.commit()
    conn.close()

def save_sensor_data(temp: float, hum: float, bat: float):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sensor_data (timestamp, temperature, humidity, battery) VALUES (?, ?, ?, ?)",
        (datetime.now(TZ).isoformat(), temp, hum, bat)
    )
    conn.commit()
    conn.close()

# ================== Weather fetch (Open-Meteo) ==================
def fetch_open_meteo(lat=10.762622, lon=106.660172):
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
            f"&forecast_days=2&timezone=auto"
        )
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data
    except Exception as e:
        logger.error(f"Open-Meteo fetch error: {e}")
        return {}

# ================== Merge weather + sensor ==================
def merge_weather_and_hours(existing_data: dict):
    data = fetch_open_meteo()
    if not data:
        return existing_data

    merged = existing_data.copy()
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # Map 6 giờ tới
    now = datetime.now(TZ)
    for i in range(6):
        if i < len(times):
            t = datetime.fromisoformat(times[i]).astimezone(TZ)
            merged[f"hour_{i}_time"] = t.strftime("%H:%M")
            merged[f"hour_{i}_temperature"] = hourly.get("temperature_2m", [None])[i]
            code_raw = hourly.get("weathercode", [None])[i]
            merged[f"hour_{i}_weather_desc"] = WEATHER_CODE_MAP.get(code_raw, str(code_raw))

    # Daily info
    daily = data.get("daily", {})
    merged["today_temp_max"] = daily.get("temperature_2m_max", [None])[0]
    merged["today_temp_min"] = daily.get("temperature_2m_min", [None])[0]
    merged["today_precipitation"] = daily.get("precipitation_sum", [None])[0]

    return merged

# ================== ThingsBoard push ==================
def sanitize_for_tb(data: dict):
    clean = {}
    for k, v in data.items():
        if isinstance(v, (int, float, str)):
            clean[k] = v
        elif isinstance(v, (list, dict)):
            clean[k] = json.dumps(v, ensure_ascii=False)
    return clean

def send_to_thingsboard(data: dict):
    try:
        sanitized = sanitize_for_tb(data)
        logger.info(f"[TB PUSH] {json.dumps(sanitized, ensure_ascii=False)}")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] {r.status_code} {r.text}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== FastAPI ==================
app = FastAPI(title="Agri-Bot")

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"[RX] Sensor data: {data}")
    save_sensor_data(data.temperature, data.humidity, data.battery)
    merged = merge_weather_and_hours(data.dict())
    send_to_thingsboard(merged)
    return {"status": "ok", "merged": merged}

@app.get("/")
def root():
    return {"msg": "Agri-Bot API running"}

# ================== Background Auto-loop ==================
async def auto_loop():
    while True:
        try:
            sample = {
                "temperature": round(random.uniform(20, 35), 1),
                "humidity": round(random.uniform(50, 95), 1),
                "battery": round(random.uniform(3.7, 4.2), 3),
            }
            logger.info(f"[AUTO] Sample ▶ {sample}")
            merged = merge_weather_and_hours(sample)
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"[AUTO] Loop error: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(auto_loop())
    logger.info("✅ Auto-loop simulator started")

# ================== Main ==================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
