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
CROP = "Rau muống"

if not TB_TOKEN or not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing TB_TOKEN or WEATHER_API_KEY in environment variables!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")
logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

# ================== APP ==================
app = FastAPI()

# ================== WEATHER MAPPING ==================
weather_mapping_vi = {
    "Sunny": "Nắng nhẹ / Nắng ấm",
    "Clear": "Trời quang",
    "Partly cloudy": "Trời ít mây",
    "Cloudy": "Có mây",
    "Overcast": "Trời âm u",
    "Mist": "Sương mù nhẹ",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to / Mưa lớn",
    "Torrential rain shower": "Mưa rất to / Kéo dài",
    "Patchy light rain with thunder": "Mưa rào kèm dông / Mưa dông",
    "Moderate or heavy rain with thunder": "Mưa rào kèm dông / Mưa dông",
    "Patchy rain nearby": "Có mưa cục bộ",
    "Thundery outbreaks possible": "Có thể có dông",
    "Fog": "Sương mù",
}

def translate_condition(cond: str) -> str:
    return weather_mapping_vi.get(cond, cond)

# ================== FUNCTIONS ==================
def fetch_weather():
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_KEY}&q={LOCATION}&days=2&aqi=no&alerts=no"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Dự báo 4-7 giờ tới (hourly)
        forecast_hours = []
        now_hour = datetime.utcnow().hour
        for i in range(4, 8):
            hour_idx = i
            if hour_idx >= len(data["forecast"]["forecastday"][0]["hour"]):
                break
            h = data["forecast"]["forecastday"][0]["hour"][hour_idx]
            forecast_hours.append({
                f"hour_{i-4}_temperature": h["temp_c"],
                f"hour_{i-4}_humidity": h["humidity"],
                f"hour_{i-4}_weather_desc": translate_condition(h["condition"]["text"]),
                f"hour_{i-4}_weather_desc_en": h["condition"]["text"]
            })

        # Hôm qua, hôm nay, ngày mai
        today = data["forecast"]["forecastday"][0]["day"]
        tomorrow = data["forecast"]["forecastday"][1]["day"]
        yesterday_weather_desc = "Không có dữ liệu"  # WeatherAPI free không cung cấp ngày trước
        telemetry = {
            "time": datetime.utcnow().isoformat(),
            "location": LOCATION,
            "crop": CROP,
            "temperature_today_min": today["mintemp_c"],
            "temperature_today_max": today["maxtemp_c"],
            "humidity_today_avg": today["avghumidity"],
            "weather_today_desc": translate_condition(today["condition"]["text"]),
            "weather_today_desc_en": today["condition"]["text"],
            "temperature_tomorrow_min": tomorrow["mintemp_c"],
            "temperature_tomorrow_max": tomorrow["maxtemp_c"],
            "humidity_tomorrow_avg": tomorrow["avghumidity"],
            "weather_tomorrow_desc": translate_condition(tomorrow["condition"]["text"]),
            "weather_tomorrow_desc_en": tomorrow["condition"]["text"],
            "temperature_yesterday_min": None,
            "temperature_yesterday_max": None,
            "humidity_yesterday_avg": None,
            "weather_yesterday_desc": yesterday_weather_desc,
            "weather_yesterday_desc_en": yesterday_weather_desc
        }

        # Gộp forecast hours
        for h in forecast_hours:
            telemetry.update(h)

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
    job()

# ================== ENDPOINTS ==================
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/last-push")
async def last_push():
    telemetry = fetch_weather()
    return telemetry
