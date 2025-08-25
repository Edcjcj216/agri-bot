import os
import requests
import httpx
import logging
from datetime import datetime
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
OWM_API_KEY = os.getenv("OWM_API_KEY")  # OpenWeather API key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # AI API key
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # fallback AI API key
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")  # ThingsBoard device token

LAT = 10.8781
LON = 106.7594

logging.basicConfig(level=logging.INFO)

app = FastAPI()
scheduler = BackgroundScheduler()

# ================== THINGSBOARD ==================
def push_telemetry(payload: dict):
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        logging.info(f"[INFO] Pushing telemetry: {payload}")
        resp = requests.post(url, json=payload)
        logging.info(f"[INFO] Response status: {resp.status_code}, body: {resp.text}")
    except Exception as e:
        logging.error(f"[ERROR] Failed to push telemetry: {e}")

# ================== OPENWEATHER ==================
def fetch_weather():
    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/onecall"
            f"?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&exclude=minutely,daily,alerts"
        )
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current", {})
        hourly = data.get("hourly", [])
        return current, hourly[:7]  # 7 hours
    except Exception as e:
        logging.error(f"[ERROR] Error fetching OpenWeather: {e}")
        return None, None

# ================== AI ADVICE ==================
def generate_weather_advice(current, hourly):
    try:
        prompt = f"""
        Dữ liệu thời tiết hiện tại và sắp tới:
        Current: {current}
        Hourly: {hourly}
        Hãy đưa ra dự báo + khuyến cáo chăm sóc rau muống (nhiệt độ, độ ẩm, mưa, gió) dưới dạng dict với keys:
        advice, advice_care, advice_nutrition, advice_note
        """
        # Ví dụ dùng Gemini/OpenRouter, tùy bạn chọn API
        headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
        resp = requests.post(
            "https://api.openrouter.ai/v1/completions",
            headers=headers,
            json={
                "model": "gemini-1",
                "prompt": prompt,
                "max_tokens": 300,
            },
            timeout=10,
        )
        resp.raise_for_status()
        advice = resp.json().get("choices", [{}])[0].get("text", "")
        return {"advice": advice}
    except Exception as e:
        logging.error(f"[ERROR] Error generating AI advice: {e}")
        return {"advice": "AI advice failed"}

# ================== JOB ==================
def job():
    current, hourly = fetch_weather()
    if not current or not hourly:
        logging.error("[ERROR] Skipping job due to weather fetch failure")
        return

    telemetry = {}

    # Current
    telemetry["temperature"] = current.get("temp")
    telemetry["humidity"] = current.get("humidity")
    telemetry["prediction"] = f"Nhiệt độ {current.get('temp')}°C, độ ẩm {current.get('humidity')}%"

    # Hourly
    for i, h in enumerate(hourly):
        telemetry[f"hour_{i}"] = datetime.fromtimestamp(h.get("dt")).strftime("%H:%M")
        telemetry[f"hour_{i}_temperature"] = h.get("temp")
        telemetry[f"hour_{i}_humidity"] = h.get("humidity")
        telemetry[f"hour_{i}_weather_desc"] = h.get("weather", [{}])[0].get("description", "")

    # AI advice
    advice = generate_weather_advice(current, hourly)
    telemetry.update(advice)

    # Push to ThingsBoard
    push_telemetry(telemetry)

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logging.info("[INFO] Starting app...")
    # push startup ping số để ThingsBoard nhận
    push_telemetry({"startup_ping": 1})
    # gọi ngay job để ThingsBoard có dữ liệu đầu tiên
    job()
    # schedule job mỗi 5 phút
    scheduler.add_job(job, "interval", minutes=5)
    scheduler.start()

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)), log_level="info")
