import os
import json
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")

if not TB_TOKEN:
    raise RuntimeError("⚠️ Missing TB_TOKEN in environment variables!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

# ================== APP ==================
app = FastAPI()

# WeatherAPI key & location
WEATHER_KEY = os.getenv("WEATHER_KEY")
LOCATION = os.getenv("LOCATION", "Ho Chi Minh,VN")

if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_KEY in environment variables!")

# ================== WEATHER MAPPING ==================
weather_mapping = {
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Trời ít mây",
    "Cloudy": "Có mây",
    "Overcast": "Trời âm u",
    "Mist": "Sương mù nhẹ",
    "Patchy rain possible": "Có thể có mưa",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Torrential rain shower": "Mưa rất to",
    "Thundery outbreaks possible": "Có thể có dông",
    "Patchy light rain with thunder": "Mưa nhẹ kèm dông",
    "Moderate or heavy rain with thunder": "Mưa to kèm dông",
    "Fog": "Sương mù",
}

def translate_condition(cond: str) -> str:
    return weather_mapping.get(cond, cond)

# ================== FUNCTIONS ==================
def fetch_weather():
    url = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_KEY}&q={LOCATION}&aqi=no"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        telemetry = {
            "time": datetime.utcnow().isoformat(),
            "location": LOCATION,
            "temperature": data["current"]["temp_c"],
            "humidity": data["current"]["humidity"],
            "weather_desc": translate_condition(data["current"]["condition"]["text"]),
            "crop": "Rau muống"
        }
        return telemetry
    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None

def push_thingsboard(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=10)
        r.raise_for_status()
        logger.info(f"✅ Pushed telemetry: {payload}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")

def job():
    telemetry = fetch_weather()
    if telemetry:
        push_thingsboard(telemetry)

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()
scheduler.add_job(job, "interval", minutes=5)
scheduler.start()

# ================== STARTUP ACTION ==================
@app.on_event("startup")
def startup_event():
    logger.info("🚀 Service started, pushing startup telemetry...")
    push_thingsboard({"startup": True, "time": datetime.utcnow().isoformat()})
    job()

# ================== ENDPOINTS ==================
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/last-push")
async def last_push():
    telemetry = fetch_weather()
    return telemetry
