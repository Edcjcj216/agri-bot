import os
import json
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")
WEATHER_KEY = os.getenv("WEATHER_API_KEY")
LOCATION = os.getenv("LOCATION", "Ho Chi Minh,VN")

if not TB_TOKEN:
    raise RuntimeError("⚠️ Missing TB_TOKEN in environment variables!")
if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_API_KEY in environment variables!")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")
logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

# ================== APP ==================
app = FastAPI()

# ================== WEATHER MAPPING ==================
weather_mapping = {
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Trời ít mây",
    "Cloudy": "Có mây",
    "Overcast": "Trời âm u",
    "Mist": "Sương mù nhẹ",
    "Patchy rain possible": "Có thể có mưa",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Torrential rain shower": "Mưa rất to",
    "Thundery outbreaks possible": "Có thể có dông",
    "Patchy light rain with thunder": "Mưa nhẹ kèm dông",
    "Moderate or heavy rain with thunder": "Mưa to kèm dông",
    "Fog": "Sương mù",
    "Patchy rain nearby": "Có mưa cục bộ",
    "Patchy light rain": "Mưa nhẹ",
    "Light drizzle": "Mưa phùn nhẹ",
    "Heavy drizzle": "Mưa phùn nặng",
    "Thunderstorm": "Dông",
}

def translate_condition(cond: str) -> str:
    return weather_mapping.get(cond, cond)

# ================== FUNCTIONS ==================
def fetch_weather():
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_KEY}&q={LOCATION}&days=2&aqi=no&alerts=no"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        telemetry = {
            "time": datetime.utcnow().isoformat(),
            "location": LOCATION,
            "temperature": data["current"]["temp_c"],
            "humidity": data["current"]["humidity"],
            "pressure_mb": data["current"]["pressure_mb"],
            "rain_1h_mm": data["current"]["precip_mm"],
            "uv_index": data["current"]["uv"],
            "visibility_km": data["current"]["vis_km"],
            "wind_kph": data["current"]["wind_kph"],
            "wind_gust_kph": data["current"]["gust_kph"],
            "weather_desc": translate_condition(data["current"]["condition"]["text"]),
            "weather_desc_en": data["current"]["condition"]["text"],
        }

        # 4-7 giờ tới
        for i, hour_data in enumerate(data["forecast"]["forecastday"][0]["hour"][:7]):
            telemetry[f"hour_{i}_temperature"] = hour_data["temp_c"]
            telemetry[f"hour_{i}_humidity"] = hour_data["humidity"]
            telemetry[f"hour_{i}_weather_desc"] = translate_condition(hour_data["condition"]["text"])
            telemetry[f"hour_{i}_weather_desc_en"] = hour_data["condition"]["text"]

        # Hôm qua
        yesterday = data["forecast"]["forecastday"][0]
        telemetry.update({
            "weather_yesterday_desc": translate_condition(yesterday["day"]["condition"]["text"]),
            "weather_yesterday_max": yesterday["day"]["maxtemp_c"],
            "weather_yesterday_min": yesterday["day"]["mintemp_c"],
            "humidity_yesterday": yesterday["day"]["avghumidity"],
        })

        # Hôm nay
        today = data["forecast"]["forecastday"][0]
        telemetry.update({
            "weather_today_desc": translate_condition(today["day"]["condition"]["text"]),
            "weather_today_desc_en": today["day"]["condition"]["text"],
            "weather_today_max": today["day"]["maxtemp_c"],
            "weather_today_min": today["day"]["mintemp_c"],
            "humidity_today": today["day"]["avghumidity"],
        })

        # Ngày mai
        tomorrow = data["forecast"]["forecastday"][1]
        telemetry.update({
            "weather_tomorrow_desc": translate_condition(tomorrow["day"]["condition"]["text"]),
            "weather_tomorrow_desc_en": tomorrow["day"]["condition"]["text"],
            "weather_tomorrow_max": tomorrow["day"]["maxtemp_c"],
            "weather_tomorrow_min": tomorrow["day"]["mintemp_c"],
            "humidity_tomorrow": tomorrow["day"]["avghumidity"],
        })

        return telemetry

    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None

def push_thingsboard(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=10)
        r.raise_for_status()
        logger.info(f"✅ Pushed telemetry: {payload}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")

def job():
    telemetry = fetch_weather()
    if telemetry:
        push_thingsboard(telemetry)

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()
scheduler.add_job(job, "interval", minutes=5)
scheduler.start()

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logger.info("🚀 Service started, pushing startup telemetry...")
    push_thingsboard({"startup": True, "time": datetime.utcnow().isoformat()})
    job()

# ================== ENDPOINTS ==================
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/last-push")
async def last_push():
    telemetry = fetch_weather()
    return telemetry
