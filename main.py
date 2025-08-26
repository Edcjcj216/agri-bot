import os
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

# ================== CONFIG ==================
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "your_weatherapi_key_here")
LAT, LON = 10.7769, 106.7009  # Ho Chi Minh City
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")

POLL_INTERVAL = 300  # 5 phút
FORECAST_BIAS = 7  # shift múi giờ dự báo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()
last_push = {}

# ================== WEATHER CONDITION MAPPING ==================
WEATHER_MAP = {
    "Sunny": "Có nắng",
    "Clear": "Có nắng",
    "Partly cloudy": "Nắng nhẹ",
    "Cloudy": "Có mây",
    "Overcast": "U ám",
    "Mist": "Sương mù",
    "Fog": "Sương mù",
    "Patchy rain possible": "Có mưa rào",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Moderate or heavy rain with thunder": "Mưa giông",
    "Thundery outbreaks possible": "Có giông",
    "Thunderstorm": "Giông bão",
    "Light snow": "Không xác định",
    "Moderate snow": "Không xác định",
    "Heavy snow": "Không xác định",
    "Ice pellets": "Không xác định",
    "Freezing rain": "Không xác định",
    "Other": "Không xác định",
}

def map_condition(raw_text: str, maxtemp: float, wind: float) -> str:
    """Map điều kiện thời tiết + override"""
    condition = WEATHER_MAP.get(raw_text, "Không xác định")

    # Override: Nắng nóng
    if maxtemp >= 35:
        return "Nắng nóng"

    # Override: Mưa bão
    if "mưa" in condition.lower() and wind >= 40:
        return "Mưa bão"

    return condition


# ================== FETCH + PUSH ==================
def fetch_weather():
    try:
        url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={LAT},{LON}&days=1&aqi=no&alerts=no&lang=vi"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None


def push_to_tb(payload: dict):
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        logger.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logger.error(f"[ERROR] Push TB: {e}")


def job():
    global last_push
    data = fetch_weather()
    if not data:
        return

    current = data["current"]
    forecast_day = data["forecast"]["forecastday"][0]["day"]
    hours = data["forecast"]["forecastday"][0]["hour"]

    maxtemp = forecast_day["maxtemp_c"]
    wind = current["wind_kph"]

    condition_text = map_condition(current["condition"]["text"], maxtemp, wind)

    payload = {
        "temperature": current["temp_c"],
        "humidity": current["humidity"],
        "weather_desc": condition_text,
        "daily_max": maxtemp,
        "daily_min": forecast_day["mintemp_c"],
        "last_update": datetime.utcnow().isoformat()
    }

    # Add next 4 hours forecast
    now_hour = datetime.utcnow().hour + FORECAST_BIAS
    for i in range(5):
        idx = (now_hour + i) % 24
        if idx < len(hours):
            h = hours[idx]
            payload[f"hour_{i}_temperature"] = h["temp_c"]
            payload[f"hour_{i}_humidity"] = h["humidity"]
            payload[f"hour_{i}_weather_desc"] = map_condition(
                h["condition"]["text"], forecast_day["maxtemp_c"], h["wind_kph"]
            )

    last_push = payload
    push_to_tb(payload)


# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    scheduler = BackgroundScheduler()
    scheduler.add_job(job, "interval", seconds=POLL_INTERVAL)
    scheduler.start()
    logger.info("🌤 Weather job scheduler started")
    job()  # chạy ngay lần đầu


@app.get("/")
def root():
    return {"status": "ok", "msg": "WeatherAPI → ThingsBoard bridge"}


@app.get("/last-push")
def get_last_push():
    return last_push
