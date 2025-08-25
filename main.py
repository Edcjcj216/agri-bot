import os
import json
import logging
import requests
import httpx
from datetime import datetime
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")  # ThingsBoard Device Token
OWM_API_KEY = os.getenv("OWM_API_KEY")  # OpenWeatherMap API Key
LAT = os.getenv("LAT", "10.8781")
LON = os.getenv("LON", "106.7594")
USE_OPENROUTER = bool(os.getenv("USE_OPENROUTER", "0") == "1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="AgriBot Telemetry API")
scheduler = BackgroundScheduler()

# ================== HELPERS ==================
def fetch_weather():
    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/onecall?"
            f"lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&exclude=minutely,daily,alerts"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logging.info(f"OpenWeather fetched: {data['current']}")
        return data
    except Exception as e:
        logging.error(f"[ERROR] Error fetching OpenWeather: {e}")
        return None

def generate_weather_advice(weather_data):
    """
    Gọi AI để sinh dự báo dựa trên dữ liệu weather_data
    """
    prompt = {
        "temperature": weather_data['current']['temp'],
        "humidity": weather_data['current']['humidity'],
        "weather_today": weather_data['current']['weather'][0]['description'],
    }

    advice = f"Nhiệt độ {prompt['temperature']}°C, độ ẩm {prompt['humidity']}%, dự báo: {prompt['weather_today']}"
    # TODO: tích hợp Gemini/OpenRouter API nếu cần
    return advice

def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        logging.info(f"[INFO] Pushing telemetry: {payload}")
        resp = requests.post(url, json=payload, timeout=10)
        logging.info(f"[INFO] Response status: {resp.status_code}, body: {resp.text}")
        return resp
    except Exception as e:
        logging.error(f"[ERROR] Failed to push telemetry: {e}")
        return None

# ================== JOB ==================
def job():
    weather = fetch_weather()
    if not weather:
        logging.error("[ERROR] Skipping job due to weather fetch failure")
        return

    advice = generate_weather_advice(weather)
    payload = {
        "temperature": weather['current']['temp'],
        "humidity": weather['current']['humidity'],
        "weather_today_desc": weather['current']['weather'][0]['description'],
        "prediction": advice,
        "timestamp": datetime.utcnow().isoformat()
    }
    push_telemetry(payload)

# ================== STARTUP ==================
@app.on_event("startup")
async def startup_event():
    logging.info("[INFO] Starting app...")
    # Push startup ping
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
    # Start job scheduler
    scheduler.add_job(job, 'interval', minutes=5, id="weather_job")
    scheduler.start()

# ================== ROUTES ==================
@app.get("/")
async def root():
    return {"status": "AgriBot running"}

