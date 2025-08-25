import os
import json
import logging
from datetime import datetime
import requests
import httpx
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== CONFIG ==================
OWM_API_KEY = os.getenv("OWM_API_KEY")
LAT = os.getenv("LAT", "10.8781")
LON = os.getenv("LON", "106.7594")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

TB_URL = os.getenv("TB_URL", "https://thingsboard.cloud/api/v1")
TB_TOKEN = os.getenv("TB_TOKEN")

CROP = "Rau muống"

app = FastAPI()
scheduler = BackgroundScheduler()

# ================== FUNCTIONS ==================

def fetch_weather():
    """Fetch hourly weather from OpenWeather free endpoint."""
    try:
        url = f"https://api.openweathermap.org/data/2.5/onecall?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&exclude=minutely,daily,alerts"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        hourly = data.get("hourly", [])[:7]  # next 7 hours
        weather = []
        for i, h in enumerate(hourly):
            weather.append({
                f"hour_{i}": datetime.utcfromtimestamp(h["dt"]).strftime("%H:%M"),
                f"hour_{i}_temperature": h["temp"],
                f"hour_{i}_humidity": h["humidity"],
                f"hour_{i}_weather_desc": h["weather"][0]["description"]
            })
        return weather
    except Exception as e:
        logger.error(f"[ERROR] Error fetching OpenWeather: {e}")
        return None

def fetch_ai_advice(weather_data):
    """Call Gemini/OpenRouter AI to generate advice."""
    prompt = f"Crop: {CROP}\nWeather data: {json.dumps(weather_data)}\nProvide advice (nutrition, care, note)."
    headers_gemini = {"Authorization": f"Bearer {GEMINI_API_KEY}"} if GEMINI_API_KEY else {}
    headers_or = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"} if OPENROUTER_API_KEY else {}
    
    # Try Gemini
    if GEMINI_API_KEY:
        try:
            r = httpx.post("https://api.gemini.com/v1/generate", json={"prompt": prompt}, headers=headers_gemini, timeout=10)
            r.raise_for_status()
            result = r.json().get("text")
            if result:
                return parse_ai_advice(result)
        except Exception as e:
            logger.warning(f"[WARNING] Gemini API failed: {e}")
    
    # Try OpenRouter
    if OPENROUTER_API_KEY:
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                           json={"model":"gpt-4o-mini","messages":[{"role":"user","content":prompt}]},
                           headers=headers_or, timeout=10)
            r.raise_for_status()
            choices = r.json().get("choices", [])
            if choices:
                return parse_ai_advice(choices[0]["message"]["content"])
        except Exception as e:
            logger.warning(f"[WARNING] OpenRouter API failed: {e}")

    return {"advice": "No AI advice available", "advice_care": "", "advice_nutrition": "", "advice_note": ""}

def parse_ai_advice(text):
    """Split AI advice into sections."""
    return {
        "advice": text,
        "advice_care": text,
        "advice_nutrition": text,
        "advice_note": ""
    }

def push_telemetry(payload):
    """Push payload to ThingsBoard."""
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        headers = {"Content-Type": "application/json"}
        logger.info(f"[INFO] Pushing telemetry: {payload}")
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"[INFO] Response status: {r.status_code}, body: {r.text}")
    except Exception as e:
        logger.error(f"[ERROR] ThingsBoard push failed: {e}")

def job():
    """Scheduled job to fetch weather, AI advice, and push telemetry."""
    weather_data = fetch_weather()
    if not weather_data:
        logger.error("[ERROR] Skipping job due to weather fetch failure")
        return

    ai_advice = fetch_ai_advice(weather_data)
    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "location": f"Lat {LAT}, Lon {LON}",
        "crop": CROP,
        "battery": 4.2,
    }

    # Flatten hourly weather
    for h in weather_data:
        payload.update(h)

    payload.update(ai_advice)
    push_telemetry(payload)

# ================== STARTUP ==================

@app.on_event("startup")
def startup_event():
    logger.info("[INFO] Starting app...")
    # Push startup ping
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
    # Add scheduler job every 15 min
    scheduler.add_job(job, 'interval', minutes=15, next_run_time=datetime.utcnow())
    scheduler.start()

# ================== HEALTH CHECK ==================

@app.get("/")
def read_root():
    return {"status": "ok"}

