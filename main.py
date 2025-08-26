import os
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN", "your_tb_token_here")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "your_weatherapi_key_here")

LAT, LON = 10.7769, 106.7009  # Hồ Chí Minh mặc định

# Mapping điều kiện thời tiết -> tiếng Việt (chỉ giữ cái phù hợp VN)
WEATHER_MAP = {
    "Sunny": "Trời nắng",
    "Clear": "Trời quang đãng",
    "Partly cloudy": "Có mây",
    "Cloudy": "Nhiều mây",
    "Overcast": "U ám",
    "Mist": "Sương mù",
    "Fog": "Sương mù dày",
    "Patchy rain possible": "Có thể có mưa",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Torrential rain shower": "Mưa lớn dữ dội",
    "Thunderstorm": "Dông sấm",
    "Moderate or heavy rain with thunder": "Mưa dông",
    "Showers": "Mưa rào",
    "Patchy light rain": "Mưa nhỏ rải rác",
}

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ================== FASTAPI ==================
app = FastAPI()

def fetch_weather():
    """Gọi WeatherAPI"""
    url = (
        f"http://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHER_API_KEY}&q={LAT},{LON}&days=1&aqi=no&alerts=no"
    )
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()

        current = data["current"]
        condition_en = current["condition"]["text"]
        condition_vi = WEATHER_MAP.get(condition_en, condition_en)

        weather = {
            "temperature": current["temp_c"],
            "humidity": current["humidity"],
            "weather_desc": condition_vi,
        }
        logger.info(f"🌤 Weather fetched: {weather}")
        return weather
    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None

def push_thingsboard(payload: dict):
    """Đẩy telemetry lên ThingsBoard"""
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
        logger.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")

def job():
    weather = fetch_weather()
    if weather:
        push_thingsboard(weather)

@app.on_event("startup")
def startup_event():
    # Debug key (chỉ hiện 4 ký tự đầu để check)
    logger.info(f"🔑 WEATHER_API_KEY = {WEATHER_API_KEY[:4]}***")
    logger.info(f"🔑 TB_TOKEN = {TB_TOKEN[:4]}***")

    scheduler = BackgroundScheduler()
    scheduler.add_job(job, "interval", minutes=5)
    scheduler.start()
    job()  # chạy 1 lần ngay khi start

@app.get("/")
def root():
    return {"status": "ok", "message": "WeatherAPI -> ThingsBoard running"}

@app.get("/last-push")
def last_push():
    return fetch_weather()
