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
LAT = 10.7769   # Hồ Chí Minh
LON = 106.7009
FORECAST_BIAS = 0  # giờ offset nếu cần chỉnh lệch dự báo

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ================== FASTAPI ==================
app = FastAPI()
last_push_data = {}

# ================== WEATHER MAPPING ==================
def map_weather_to_vn(condition_text: str, daily_max: float = None) -> str:
    text = condition_text.lower()

    # Override Nắng nóng
    if daily_max is not None and daily_max >= 35:
        return "Nắng nóng"

    # Override Mưa bão
    if "torrential rain" in text or "heavy rain with thunder" in text:
        return "Mưa bão"

    mapping = {
        "sunny": "Có nắng",
        "clear": "Trời quang",
        "partly cloudy": "Nắng nhẹ",
        "cloudy": "Nhiều mây",
        "overcast": "Âm u",
        "mist": "Sương mù",
        "fog": "Sương mù",
        "light rain": "Mưa nhẹ",
        "patchy light rain": "Mưa nhẹ",
        "patchy rain possible": "Mưa nhẹ",
        "moderate rain": "Mưa vừa",
        "moderate rain at times": "Mưa vừa",
        "heavy rain": "Mưa to",
        "heavy rain at times": "Mưa to",
        "rain shower": "Mưa rào",
        "light rain shower": "Mưa rào",
        "showers": "Mưa rào",
        "thunder": "Dông",
        "thunderstorm": "Mưa giông",
        "patchy light rain with thunder": "Mưa giông",
        "moderate or heavy rain with thunder": "Mưa giông",
    }

    for key, val in mapping.items():
        if key in text:
            return val

    return "Không xác định"

# ================== WEATHER FETCH ==================
def fetch_weather():
    try:
        url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={LAT},{LON}&days=1&aqi=no&alerts=no"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()

        current = data["current"]
        forecast_day = data["forecast"]["forecastday"][0]
        daily_max = forecast_day["day"]["maxtemp_c"]

        telemetry = {
            "temperature": current["temp_c"],
            "humidity": current["humidity"],
            "weather_desc": map_weather_to_vn(current["condition"]["text"], daily_max),
            "daily_max": daily_max,
            "_ts": int(datetime.utcnow().timestamp() * 1000),
        }

        # Thêm dự báo giờ (hour_0..hour_4)
        for i in range(5):
            hour = forecast_day["hour"][i + FORECAST_BIAS]
            telemetry[f"hour_{i}temperature"] = hour["temp_c"]
            telemetry[f"hour_{i}humidity"] = hour["humidity"]
            telemetry[f"hour_{i}weather_desc"] = map_weather_to_vn(hour["condition"]["text"], daily_max)

        return telemetry

    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None

# ================== PUSH TO THINGSBOARD ==================
def push_thingsboard(payload: dict):
    global last_push_data
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
        last_push_data = payload
        logger.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")

# ================== JOB ==================
def job():
    weather = fetch_weather()
    if weather:
        push_thingsboard(weather)

# ================== STARTUP ==================
scheduler = BackgroundScheduler()
scheduler.add_job(job, "interval", minutes=5)
scheduler.start()

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Service started, pushing first telemetry...")
    job()

@app.get("/")
async def root():
    return {"status": "running", "last_push": last_push_data}

@app.get("/last-push")
async def last_push():
    return last_push_data
