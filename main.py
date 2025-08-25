import os
import json
import requests
import httpx
from datetime import datetime
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
OWM_API_KEY = os.getenv("OWM_API_KEY")  # OpenWeatherMap API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # AI key
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # AI key
TB_TOKEN = os.getenv("THINGSBOARD_TOKEN")
LAT = 10.8781
LON = 106.7594

TB_URL = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

app = FastAPI()
scheduler = BackgroundScheduler()

# ================== FUNCTIONS ==================
def push_telemetry(payload: dict):
    try:
        print(f"[INFO] Pushing telemetry: {payload}")
        resp = requests.post(TB_URL, json=payload)
        print(f"[INFO] Response status: {resp.status_code}, body: {resp.text}")
        return resp
    except Exception as e:
        print(f"[ERROR] Failed to push telemetry: {e}")
        return None

def fetch_openweather():
    if not OWM_API_KEY:
        print("[ERROR] OWM_API_KEY not set")
        return None

    url = (
        f"https://api.openweathermap.org/data/2.5/onecall?"
        f"lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&exclude=minutely,daily,alerts"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Extract next 7 hours forecast
        hourly_data = data.get("hourly", [])[:7]
        forecast = []
        for i, h in enumerate(hourly_data):
            forecast.append({
                "hour": datetime.utcfromtimestamp(h["dt"]).strftime("%H:00"),
                "temperature": round(h["temp"], 1),
                "humidity": h.get("humidity", 0),
                "weather_desc": h.get("weather", [{}])[0].get("description", "")
            })
        return forecast
    except Exception as e:
        print(f"[ERROR] Error fetching OpenWeather: {e}")
        return None

def generate_weather_advice(forecast):
    # Simple AI prompt for Gemini/OpenRouter
    if not forecast:
        return {"advice": "No forecast data"}
    # Compose prompt
    prompt = "Dựa trên dữ liệu thời tiết sau, đưa ra lời khuyên chăm sóc rau muống:\n"
    for f in forecast:
        prompt += f"{f['hour']}: Temp {f['temperature']}°C, Humidity {f['humidity']}%, {f['weather_desc']}\n"
    prompt += "\nLời khuyên:"
    # Call AI (Gemini or OpenRouter)
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY or OPENROUTER_API_KEY}"}
    try:
        r = requests.post(
            "https://api.openrouter.ai/v1/chat/completions",
            headers=headers,
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7
            },
            timeout=15
        )
        r.raise_for_status()
        resp_json = r.json()
        advice_text = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"advice": advice_text}
    except Exception as e:
        print(f"[ERROR] AI request failed: {e}")
        return {"advice": "AI request failed"}

def job():
    forecast = fetch_openweather()
    if not forecast:
        print("[WARN] No forecast, skipping telemetry push")
        return
    advice = generate_weather_advice(forecast)
    payload = {"forecast": forecast, **advice, "timestamp": datetime.utcnow().isoformat()}
    push_telemetry(payload)

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    print("[INFO] Starting app...")
    # push startup ping
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
    # schedule job every 5 min
    scheduler.add_job(job, "interval", minutes=5)
    scheduler.start()

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "last_ping": datetime.utcnow().isoformat()}
