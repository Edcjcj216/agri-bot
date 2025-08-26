import os
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")

# Lấy key từ biến môi trường OWM_API_KEY (dùng cho WeatherAPI)
WEATHERAPI_KEY = os.getenv("OWM_API_KEY", "your_weatherapi_key_here")

if WEATHERAPI_KEY == "your_weatherapi_key_here":
    logging.warning("[WARN] WEATHERAPI_KEY chưa được set trong Render env (OWM_API_KEY).")
else:
    logging.info(f"[OK] WEATHERAPI_KEY đã load từ OWM_API_KEY (length={len(WEATHERAPI_KEY)})")

LAT, LON = 10.7769, 106.7009  # HCM City
DEVICE_TOKEN = TB_TOKEN

# ================== FASTAPI ==================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "time": datetime.utcnow()}

@app.head("/")
async def head_root():
    return {}

# ================== WEATHER JOB ==================
def fetch_weather():
    url = (
        f"http://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHERAPI_KEY}&q={LAT},{LON}&days=1&aqi=no&alerts=no&lang=vi"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        current = data.get("current", {})
        forecast_today = data.get("forecast", {}).get("forecastday", [{}])[0].get("day", {})

        telemetry = {
            "temperature": current.get("temp_c"),
            "humidity": current.get("humidity"),
            "prediction": f"Nhiệt độ {current.get('temp_c')}°C, độ ẩm {current.get('humidity')}%",
            "weather_today_min": forecast_today.get("mintemp_c"),
            "weather_today_max": forecast_today.get("maxtemp_c"),
            "weather_today_desc": forecast_today.get("condition", {}).get("text"),
            "time": datetime.utcnow().isoformat(),
        }

        push_telemetry(telemetry)
        logging.info(f"🌤 Pushed telemetry: {telemetry}")

    except Exception as e:
        logging.error(f"[ERROR] Fetch WeatherAPI: {e}")

def push_telemetry(payload: dict):
    try:
        url = f"{TB_URL}/{DEVICE_TOKEN}/telemetry"
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logging.error(f"[ERROR] Push ThingsBoard: {e}")

# ================== STARTUP ==================
scheduler = BackgroundScheduler()

@app.on_event("startup")
async def startup_event():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    logging.info("🚀 App startup")
    logging.info("🌤 Weather job scheduler started")

    scheduler.add_job(fetch_weather, "interval", minutes=5, id="weather_job", replace_existing=True)
    scheduler.start()

    # Gửi 1 telemetry test khi khởi động
    push_telemetry({"startup": True, "time": datetime.utcnow().isoformat()})


@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()
