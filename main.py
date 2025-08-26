import os
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "your_weatherapi_key_here")
LAT = 10.7769   # HCM default
LON = 106.7009
LOCATION_NAME = "Ho Chi Minh, VN"
CROP = "Rau muống"

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ================== WEATHER MAPPING ==================
WEATHER_MAP = {
    # Nắng / Nhiệt
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Ít mây",
    "Cloudy": "Nhiều mây",
    "Overcast": "Âm u",

    # Mưa
    "Patchy light rain": "Mưa nhẹ",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Moderate or heavy rain shower": "Mưa rào vừa hoặc to",
    "Torrential rain shower": "Mưa rất to",
    "Patchy rain possible": "Có thể có mưa",

    # Dông
    "Thundery outbreaks possible": "Có dông",
    "Patchy light rain with thunder": "Mưa dông nhẹ",
    "Moderate or heavy rain with thunder": "Mưa dông to",

    # Bão / áp thấp
    "Storm": "Bão",
    "Tropical storm": "Áp thấp nhiệt đới",
}

# ================== FASTAPI APP ==================
app = FastAPI()
scheduler = BackgroundScheduler()

def fetch_weather():
    """Fetch weather data từ WeatherAPI"""
    try:
        url = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q={LAT},{LON}&aqi=no&lang=en"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        temp = data["current"]["temp_c"]
        hum = data["current"]["humidity"]
        cond = data["current"]["condition"]["text"]
        desc = WEATHER_MAP.get(cond, cond)

        telemetry = {
            "temperature": temp,
            "humidity": hum,
            "weather_desc": desc,
            "location": LOCATION_NAME,
            "crop": CROP,
            "time": datetime.utcnow().isoformat()
        }

        push_thingsboard(telemetry)
    except Exception as e:
        logger.error(f"[ERROR] Fetch weather: {e}")

def push_thingsboard(payload: dict):
    """Push telemetry lên ThingsBoard"""
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")

@app.on_event("startup")
def startup_event():
    logger.info("🚀 Service started, sending startup telemetry...")
    push_thingsboard({"startup": True, "time": datetime.utcnow().isoformat()})
    scheduler.add_job(fetch_weather, "interval", minutes=5)
    scheduler.start()

@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()

@app.get("/")
async def root():
    return {"status": "WeatherAPI → ThingsBoard running"}

@app.get("/last-push")
async def last_push():
    return {"last_push": datetime.utcnow().isoformat()}
