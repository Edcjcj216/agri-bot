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

# logging config
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("agri-bot")

# fastapi
app = FastAPI()

# database
DB_FILE = "agri_bot.db"
conn = sqlite3.connect(DB_FILE)
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS bias_history (ts TEXT, bias REAL)")
conn.commit()
conn.close()

# in-memory bias history (last 50)
bias_history = deque(maxlen=50)

# weather API
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast?"
    "latitude=10.762622&longitude=106.660172&hourly=temperature_2m,relative_humidity_2m,"
    "precipitation_probability,weathercode&forecast_days=1&timezone=auto"
)

# ThingsBoard config (from Render env)
TB_TOKEN = os.getenv("TB_TOKEN", "demoToken")
TB_URL = os.getenv("TB_URL", "https://thingsboard.cloud/api/v1/")
TB_DEVICE_URL = f"{TB_URL}{TB_TOKEN}/telemetry"

REQUEST_TIMEOUT = 10


# helper functions
def _now_local():
    if LOCAL_TZ:
        return datetime.now(LOCAL_TZ)
    return datetime.now()


def sanitize_for_tb(data: dict) -> dict:
    """Ensure telemetry keys/values are valid"""
    clean = {}
    for k, v in data.items():
        key = re.sub(r"[^a-zA-Z0-9_]", "_", str(k))
        try:
            json.dumps(v)
            clean[key] = v
        except Exception:
            clean[key] = str(v)
    return clean


def send_to_thingsboard(data: dict):
    try:
        sanitized = sanitize_for_tb(data)
        logger.info(f"[TB PUSH] Payload = {json.dumps(sanitized, ensure_ascii=False)}")
        r = requests.post(TB_DEVICE_URL, json=sanitized, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] Status = {r.status_code}, Body = {r.text}")
    except Exception as e:
        logger.error(f"[TB PUSH ERROR] {e}")


def merge_weather_and_hours(data: dict):
    try:
        now = _now_local()
        times = data.get("hourly", {}).get("time", [])
        temps = data.get("hourly", {}).get("temperature_2m", [])
        hums = data.get("hourly", {}).get("relative_humidity_2m", [])
        precs = data.get("hourly", {}).get("precipitation_probability", [])
        codes = data.get("hourly", {}).get("weathercode", [])

        hours = []
        for i, t in enumerate(times):
            ts = datetime.fromisoformat(t)
            # ép timezone local nếu thiếu
            if ts.tzinfo is None and LOCAL_TZ:
                ts = ts.replace(tzinfo=LOCAL_TZ)
            if ts >= now:
                hours.append(
                    {
                        "time": t,
                        "temperature": temps[i],
                        "humidity": hums[i],
                        "precipitation_probability": precs[i],
                        "weathercode": codes[i],
                    }
                )
            if len(hours) >= 12:
                break
        return hours
    except Exception as e:
        logger.error(f"merge_weather_and_hours error: {e}")
        return []


# FastAPI models
class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float


@app.get("/")
async def root():
    return {"status": "ok", "time": _now_local().isoformat()}


@app.get("/weather")
async def weather():
    try:
        r = requests.get(OPEN_METEO_URL, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return {"status": "ok", "hours": merge_weather_and_hours(data)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/sensor")
async def sensor(data: SensorData):
    # lưu bias vào sqlite + in-memory
    bias = data.temperature - 25.0
    ts = _now_local().isoformat()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO bias_history VALUES (?,?)", (ts, bias))
    conn.commit()
    conn.close()

    bias_history.append((ts, bias))

    payload = {"temperature": data.temperature, "humidity": data.humidity, "battery": data.battery}
    send_to_thingsboard(payload)
    return {"status": "ok", "saved_bias": bias}


@app.get("/history")
async def history():
    return list(bias_history)


@app.get("/last")
async def last_telemetry():
    if not bias_history:
        return {"status": "no data"}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT ts, bias FROM bias_history ORDER BY ts DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return {
        "status": "ok",
        "last_bias": row[1] if row else None,
        "history_len": len(bias_history),
    }


# background task for auto loop
async def auto_loop():
    await asyncio.sleep(2)
    logger.info("✅ Auto-loop simulator started")
    while True:
        try:
            sample = {
                "temperature": round(20 + random.random() * 10, 1),
                "humidity": round(60 + random.random() * 30, 1),
                "battery": round(3.7 + random.random() * 0.5, 3),
            }
            logger.info(f"[AUTO] Sensor sample ▶ {sample}")
            send_to_thingsboard(sample)

            # save bias
            bias = sample["temperature"] - 25.0
            ts = _now_local().isoformat()
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT INTO bias_history VALUES (?,?)", (ts, bias))
            conn.commit()
            conn.close()
            bias_history.append((ts, bias))

        except Exception as e:
            logger.error(f"[AUTO] Loop error: {e}")

        await asyncio.sleep(60)  # mỗi phút gửi 1 lần


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_loop())
