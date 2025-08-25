import os
import time
import logging
import requests
import httpx
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")
OWM_API_KEY = os.getenv("OWM_API_KEY", "your_openweather_api_key")
LAT, LON = 10.7758, 106.7004  # Hồ Chí Minh mặc định

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()
scheduler = BackgroundScheduler()

# ================== TELEMETRY ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        logger.info(f"[Telemetry] Sending payload: {payload}")
        r = requests.post(url, json=payload, timeout=10)
        logger.info(f"[Telemetry] Response {r.status_code}: {r.text}")
    except Exception as e:
        logger.error(f"[Telemetry] Error: {e}")

# ================== WEATHER ==================
def get_weather():
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={LAT}&lon={LON}&units=metric&lang=vi&appid={OWM_API_KEY}"
    try:
        r = httpx.get(url, timeout=10)
        data = r.json()
        now = data["list"][0]
        weather = {
            "temperature": now["main"]["temp"],
            "humidity": now["main"]["humidity"],
            "weather_desc": now["weather"][0]["description"]
        }
        return weather
    except Exception as e:
        logger.error(f"[Weather] Error fetching weather: {e}")
        return None

# ================== AI ADVICE ==================
def generate_advice(weather: dict):
    temp = weather["temperature"]
    hum = weather["humidity"]
    desc = weather["weather_desc"]

    return (
        "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ | "
        f"Nhiệt độ {temp}°C, độ ẩm {hum}% | {desc.capitalize()} | "
        "Quan sát thực tế và điều chỉnh"
    )

# ================== JOB ==================
def job():
    weather = get_weather()
    if not weather:
        return

    advice = generate_advice(weather)
    payload = {
        "temperature": weather["temperature"],
        "humidity": weather["humidity"],
        "weather_desc": weather["weather_desc"],
        "advice": advice,
        "timestamp": int(time.time())
    }
    push_telemetry(payload)

# ================== ROUTES ==================
@app.get("/")
async def root():
    return {"status": "running"}

@app.get("/test-push")
async def test_push():
    weather = get_weather()
    if not weather:
        return {"error": "weather fetch failed"}

    advice = generate_advice(weather)
    payload = {
        "temperature": weather["temperature"],
        "humidity": weather["humidity"],
        "weather_desc": weather["weather_desc"],
        "advice": advice,
        "timestamp": int(time.time()),
        "manual_test": True
    }
    push_telemetry(payload)
    return {"pushed": payload}

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    # Push startup ping
    push_telemetry({"startup_ping": int(time.time())})

    # Start scheduler
    scheduler.add_job(job, "interval", minutes=5, id="weather_job", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started: weather job every 5 minutes")