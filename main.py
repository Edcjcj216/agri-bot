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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")
logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

# ================== APP ==================
app = FastAPI()

# ================== WEATHER MAPPING TIẾNG VIỆT ==================
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
    "Patchy light rain": "Mưa nhẹ cục bộ",
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
            "weather_today_desc": translate_condition(data["forecast"]["forecastday"][0]["day"]["condition"]["text"]),
            "weather_today_max": data["forecast"]["forecastday"][0]["day"]["maxtemp_c"],
            "weather_today_min": data["forecast"]["forecastday"][0]["day"]["mintemp_c"],
            "weather_tomorrow_desc": translate_condition(data["forecast"]["forecastday"][1]["day"]["condition"]["text"]),
            "weather_tomorrow_max": data["forecast"]["forecastday"][1]["day"]["maxtemp_c"],
            "weather_tomorrow_min": data["forecast"]["forecastday"][1]["day"]["mintemp_c"],
        }

        # Dự báo 4-6 giờ tới
        for i, hour_data in enumerate(data["forecast"]["forecastday"][0]["hour"][:7]):
            telemetry[f"hour_{i}_temperature"] = hour_data["temp_c"]
            telemetry[f"hour_{i}_humidity"] = hour_data["humidity"]
            telemetry[f"hour_{i}_weather_desc"] = translate_condition(hour_data["condition"]["text"])

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

# ================== STARTUP ACTION ==================
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
