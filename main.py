import os
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN", "your_tb_token_here")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "your_weatherapi_key_here")

LAT, LON = 10.7769, 106.7009  # Hồ Chí Minh
FORECAST_BIAS = 0  # nếu muốn bù sai số nhiệt độ thì chỉnh số này
CROP_NAME = "rau muống"

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ================== APP ==================
app = FastAPI()

# ================== WEATHER MAPPING ==================
WEATHER_MAPPING = {
    "Sunny": "Có nắng",
    "Partly cloudy": "Nắng nhẹ",
    "Cloudy": "Nhiều mây",
    "Overcast": "U ám",
    "Mist": "Sương mù",
    "Fog": "Sương mù dày",
    "Patchy rain possible": "Có mưa rải rác",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Torrential rain shower": "Mưa bão",
    "Patchy thunder possible": "Có thể có giông",
    "Thundery outbreaks possible": "Có giông",
    "Moderate or heavy rain with thunder": "Mưa giông",
    "Patchy light rain with thunder": "Mưa nhỏ kèm giông",
    "Clear": "Trời quang",
}

def translate_condition(text: str, temp_c: float) -> str:
    if not text:
        return "Không xác định"
    if temp_c >= 35:  # override nắng nóng
        return "Nắng nóng"
    return WEATHER_MAPPING.get(text, "Không xác định")

# ================== FETCH WEATHER ==================
def fetch_weather():
    try:
        url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={LAT},{LON}&days=1&aqi=no&alerts=no&lang=en"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None

# ================== BUILD TELEMETRY ==================
def build_telemetry():
    data = fetch_weather()
    if not data:
        return None

    current = data.get("current", {})
    forecast = data.get("forecast", {}).get("forecastday", [])[0].get("hour", [])

    telemetry = {
        "startup": True,
        "time": datetime.utcnow().isoformat(),
        "location": "Ho Chi Minh, VN",
        "crop": CROP_NAME,
        "temperature": round(current.get("temp_c", 0) + FORECAST_BIAS, 1),
        "humidity": current.get("humidity", 0),
        "weather_desc": translate_condition(
            current.get("condition", {}).get("text", ""),
            current.get("temp_c", 0)
        ),
    }

    # thêm 4 giờ forecast
    for i in range(5):
        if i < len(forecast):
            hour_data = forecast[i]
            telemetry[f"hour_{i}_temperature"] = round(hour_data.get("temp_c", 0) + FORECAST_BIAS, 1)
            telemetry[f"hour_{i}_humidity"] = hour_data.get("humidity", 0)
            telemetry[f"hour_{i}_weather_desc"] = translate_condition(
                hour_data.get("condition", {}).get("text", ""),
                hour_data.get("temp_c", 0)
            )
    return telemetry

# ================== PUSH TO THINGSBOARD ==================
def push_telemetry():
    telemetry = build_telemetry()
    if not telemetry:
        return
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        res = requests.post(url, json=telemetry, timeout=10)
        res.raise_for_status()
        logger.info(f"✅ Sent telemetry: {telemetry}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()
scheduler.add_job(push_telemetry, "interval", minutes=5)
scheduler.start()

# ================== FASTAPI ROUTES ==================
@app.get("/")
async def root():
    return {"status": "ok", "msg": "AgriBot WeatherAPI is running"}

@app.get("/last-push")
async def last_push():
    telemetry = build_telemetry()
    return telemetry

@app.head("/")
async def head_root():
    return {"status": "ok"}
