import os
import logging
from datetime import datetime
import httpx
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN", "")
OWM_API_KEY = os.getenv("OWM_API_KEY", "")
LAT = os.getenv("LAT", "10.8781")
LON = os.getenv("LON", "106.7594")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ================== APP ==================
app = FastAPI()

# ================== THINGSBOARD ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        logging.info(f"[INFO] Pushing telemetry: {payload}")
        response = httpx.post(url, json=payload)
        logging.info(f"[INFO] Response status: {response.status_code}, body: {response.text}")
    except Exception as e:
        logging.error(f"[ERROR] Failed to push telemetry: {e}")

# ================== FETCH OPENWEATHER ==================
def fetch_openweather():
    url = f"https://api.openweathermap.org/data/2.5/onecall?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&exclude=minutely,daily,alerts"
    try:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"[ERROR] Error fetching OpenWeather: {e}")
        return None

# ================== CALL AI ==================
def generate_ai_advice(openweather_data):
    prompt = f"""
    Dựa trên dữ liệu thời tiết sau: {openweather_data}, đưa ra dự báo nông nghiệp cho rau muống:
    - Advice dinh dưỡng
    - Advice chăm sóc
    - Note quan sát
    """
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
    data = {"prompt": prompt, "max_tokens": 300}
    try:
        r = httpx.post("https://api.openrouter.ai/v1/completions", json=data, headers=headers, timeout=20)
        r.raise_for_status()
        result = r.json()
        # Tùy API Gemini/OpenRouter, parse output ở đây
        advice_text = result.get("choices", [{}])[0].get("text", "Không có dữ liệu")
        # split thành care/nutrition/note giả sử có ký tự phân cách
        advice_parts = advice_text.split("|")
        return {
            "advice": advice_text,
            "advice_nutrition": advice_parts[0] if len(advice_parts) > 0 else "",
            "advice_care": advice_parts[1] if len(advice_parts) > 1 else "",
            "advice_note": advice_parts[2] if len(advice_parts) > 2 else ""
        }
    except Exception as e:
        logging.error(f"[ERROR] AI advice generation failed: {e}")
        return {
            "advice": "AI error",
            "advice_nutrition": "",
            "advice_care": "",
            "advice_note": ""
        }

# ================== JOB ==================
def job():
    logging.info("[INFO] Running scheduled job...")
    weather_data = fetch_openweather()
    if not weather_data:
        logging.error("[ERROR] Skipping job due to weather fetch failure")
        return

    # Prepare telemetry
    telemetry = {}
    # giờ hiện tại
    now_hour = datetime.now().hour
    current_weather = weather_data["hourly"][0]
    telemetry.update({
        "temperature": current_weather["temp"],
        "humidity": current_weather["humidity"],
        "weather_today_desc": weather_data["current"]["weather"][0]["description"],
        "location": f"Lat {LAT}, Lon {LON}",
    })

    # AI advice
    ai_advice = generate_ai_advice(weather_data)
    telemetry.update(ai_advice)

    # Push
    push_telemetry(telemetry)

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logging.info("[INFO] Starting app...")
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})

    # Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(job, 'interval', minutes=15, next_run_time=datetime.utcnow())
    scheduler.start()

# ================== ROOT ==================
@app.get("/")
def root():
    return {"status": "ok"}
