import os
import asyncio
import requests
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

app = FastAPI()

# Lấy từ Environment Variables trên Render
OWM_API_KEY = os.getenv("OWM_API_KEY")
LAT = os.getenv("LAT", "21.0278")   # Default: Hà Nội
LON = os.getenv("LON", "105.8342")
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")
THINGSBOARD_URL = os.getenv("THINGSBOARD_URL", "https://thingsboard.cloud")

# URL gửi telemetry
TB_TELEMETRY_URL = f"{THINGSBOARD_URL}/api/v1/{THINGSBOARD_TOKEN}/telemetry"

def fetch_weather():
    url = f"http://api.openweathermap.org/data/2.5/forecast?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&lang=vi"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()

def build_payload(data):
    telemetry = {}

    # Lấy 24h forecast (mỗi 3h từ OWM → group thành 8 cột)
    hours = {}
    for i, entry in enumerate(data["list"][:8]):  # 24h tới
        hours[f"hour{i*3}_temp"] = entry["main"]["temp"]
        hours[f"hour{i*3}_hum"] = entry["main"]["humidity"]

    # Lấy ngày mai (cột 8~15)
    tomorrow = data["list"][8:16]
    if tomorrow:
        temps = [e["main"]["temp"] for e in tomorrow]
        hums = [e["main"]["humidity"] for e in tomorrow]
        telemetry["tomorrow_temp_min"] = min(temps)
        telemetry["tomorrow_temp_max"] = max(temps)
        telemetry["tomorrow_humidity_avg"] = sum(hums) / len(hums)

    telemetry.update(hours)
    return telemetry

def push_to_thingsboard(payload):
    try:
        resp = requests.post(TB_TELEMETRY_URL, json=payload, timeout=5)
        print("Push TB:", resp.status_code, payload)
    except Exception as e:
        print("Error push:", e)

def job():
    try:
        data = fetch_weather()
        payload = build_payload(data)
        push_to_thingsboard(payload)
    except Exception as e:
        print("Job error:", e)

@app.on_event("startup")
async def startup_event():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(job, "interval", minutes=5)
    scheduler.start()

@app.get("/")
def root():
    return {"status": "Weather service running 🎉"}
