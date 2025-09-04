# main.py
# Agri-bot — Open-Meteo + sensor, dashboard-ready keys

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

# logging setup
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agribot")

# config
DB_FILE = "agribot.db"
TB_URL = os.getenv("TB_URL", "http://demo.thingsboard.io/api/v1/")
TB_TOKEN = os.getenv("TB_TOKEN", "demoToken")
AUTO_LOOP_INTERVAL = 600  # 10 minutes
WEATHER_CACHE_SECONDS = 15 * 60

app = FastAPI(title="Agri-bot")

# db init
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS bias_history (
        ts INTEGER PRIMARY KEY,
        bias REAL
    )""")
    conn.commit()
    conn.close()

bias_history = deque(maxlen=30)

def save_bias_to_db(ts, bias):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bias_history (ts, bias) VALUES (?, ?)", (ts, bias))
    conn.commit()
    conn.close()

def load_history_from_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT ts, bias FROM bias_history ORDER BY ts DESC LIMIT 30")
    rows = c.fetchall()
    conn.close()
    for _, b in rows:
        bias_history.appendleft(b)

# utils
def _now_local():
    if LOCAL_TZ:
        return datetime.now(LOCAL_TZ)
    return datetime.now()

def send_to_thingsboard(payload):
    try:
        url = f"{TB_URL}{TB_TOKEN}/telemetry"
        headers = {"Content-Type": "application/json"}
        logger.info(f"TB ▶ sending payload (keys: {list(payload.keys())})")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=5)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"TB send error: {e}")

# weather cache
_last_weather_fetch = 0
_last_weather_data = None

def fetch_weather():
    global _last_weather_fetch, _last_weather_data
    now = time.time()
    if _last_weather_data and now - _last_weather_fetch < WEATHER_CACHE_SECONDS:
        return _last_weather_data

    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=10.79&longitude=106.65"
               "&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,weathercode")
        r = requests.get(url, timeout=5)
        data = r.json()
        _last_weather_fetch = now
        _last_weather_data = data
        return data
    except Exception as e:
        logger.error(f"Weather fetch error: {e}")
        return {}

def merge_weather_and_hours(existing_data=None):
    data = fetch_weather()
    if not data:
        return existing_data or {}

    now = _now_local()
    hours = []
    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    hums = data["hourly"]["relative_humidity_2m"]
    precs = data["hourly"]["precipitation_probability"]
    codes = data["hourly"]["weathercode"]

    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t)
        if ts >= now:
            hours.append({
                "time": t,
                "temperature": temps[i],
                "humidity": hums[i],
                "precipitation_probability": precs[i],
                "weathercode": codes[i]
            })
        if len(hours) >= 12:
            break

    if existing_data is None:
        existing_data = {}
    existing_data["next_hours"] = hours
    return existing_data

def update_bias_and_correct(next_hours, current_temp):
    if not next_hours:
        return 0
    forecast_temp = next_hours[0]["temperature"]
    bias = round(current_temp - forecast_temp, 1)
    ts = int(time.time())
    bias_history.append(bias)
    save_bias_to_db(ts, bias)
    return bias

# models
class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float = 0.0

# endpoints
@app.post("/sensor")
async def receive_sensor(data: SensorData):
    weather = merge_weather_and_hours(existing_data={})
    bias = update_bias_and_correct(weather.get("next_hours", []), data.temperature)

    merged = {
        "temperature": data.temperature,
        "humidity": data.humidity,
        "battery": data.battery,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }
    merged = merge_weather_and_hours(existing_data=merged)

    send_to_thingsboard(merged)
    return {"status": "ok", "merged": merged}

@app.get("/weather")
async def weather_endpoint():
    data = fetch_weather()
    return data

# auto loop
async def auto_loop():
    logger.info("✅ Auto-loop simulator started")
    battery = 4.2
    while True:
        try:
            now = _now_local()
            hour = now.hour + now.minute / 60.0
            base = 27.0
            amplitude = 6.0

            temp = base + amplitude * math.sin((hour - 14) / 24.0 * 2 * math.pi) + random.uniform(-0.7, 0.7)
            humi = max(20.0, min(95.0, 75 - (temp - base) * 3 + random.uniform(-5, 5)))
            battery = max(3.3, battery - random.uniform(0.0005, 0.0025))

            sample = {
                "temperature": round(temp, 1),
                "humidity": round(humi, 1),
                "battery": round(battery, 3)
            }
            logger.info(f"[AUTO] Sensor sample ▶ {sample}")

            weather = merge_weather_and_hours(existing_data={})
            bias = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"])

            merged = {
                **sample,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": bias,
                "forecast_history_len": len(bias_history)
            }
            merged = merge_weather_and_hours(existing_data=merged)

            logger.info(f"[AUTO] Pushing merged ▶ temp={merged.get('temperature')}°C, "
                        f"humi={merged.get('humidity')}%, batt={merged.get('battery')}V, "
                        f"bias={merged.get('forecast_bias')} (hist={merged.get('forecast_history_len')})")

            send_to_thingsboard(merged)

        except Exception as e:
            logger.error(f"[AUTO] Loop error: {e}")

        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())

# entrypoint
if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Agri-bot FastAPI server with auto-loop enabled...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
