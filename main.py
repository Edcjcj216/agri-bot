import os
import requests
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from datetime import datetime
import pytz

# ================== CONFIG ==================
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "YOUR_OPENWEATHER_API_KEY")
THINGSBOARD_TOKEN   = os.getenv("THINGSBOARD_TOKEN", "YOUR_THINGSBOARD_DEVICE_TOKEN")
LAT = os.getenv("LAT", "10.7769")   # An Phú HCM
LON = os.getenv("LON", "106.7009")

OPENWEATHER_URL = f"https://api.openweathermap.org/data/2.5/onecall?lat={LAT}&lon={LON}&exclude=minutely,current,alerts&appid={OPENWEATHER_API_KEY}&units=metric&lang=vi"
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

# ================== FASTAPI APP ==================
app = FastAPI(title="Weather → ThingsBoard")

# ================== CORE FUNCTION ==================
def fetch_weather():
    """Lấy dữ liệu từ OpenWeather"""
    resp = requests.get(OPENWEATHER_URL)
    resp.raise_for_status()
    return resp.json()

def build_payload(data):
    """Xử lý JSON trả về thành telemetry cho ThingsBoard"""
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    payload = {}

    # 24h forecast
    for h in data["hourly"][:24]:
        hour = datetime.fromtimestamp(h["dt"], tz).strftime("%H")
        payload[f"hour_{hour}"] = {
            "temp": h["temp"],
            "humidity": h["humidity"],
            "weather": h["weather"][0]["description"]
        }

    # forecast ngày mai
    tomorrow = data["daily"][1]
    payload["tomorrow"] = {
        "temp_min": tomorrow["temp"]["min"],
        "temp_max": tomorrow["temp"]["max"],
        "humidity": tomorrow["humidity"],
        "weather": tomorrow["weather"][0]["description"]
    }

    return payload

def push_thingsboard(payload):
    """Gửi telemetry lên ThingsBoard"""
    r = requests.post(THINGSBOARD_URL, json=payload)
    r.raise_for_status()
    return r.json() if r.text else {"status": "ok"}

# ================== BACKGROUND JOB ==================
async def worker():
    while True:
        try:
            data = fetch_weather()
            payload = build_payload(data)
            push_thingsboard(payload)
            print("✅ Telemetry pushed:", datetime.now())
        except Exception as e:
            print("❌ Error:", e)
        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(worker())

# ================== API ROUTES ==================
@app.get("/")
def root():
    return {"status": "Weather → ThingsBoard service running"}

@app.get("/trigger")
def trigger():
    """Gọi tay để test push"""
    try:
        data = fetch_weather()
        payload = build_payload(data)
        result = push_thingsboard(payload)
        return JSONResponse(content={"sent": payload, "result": result})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
