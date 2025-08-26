import os
import json
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")  # Token ThingsBoard

if not TB_TOKEN:
    raise RuntimeError("⚠️ Missing TB_TOKEN in environment variables!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")
logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

app = FastAPI()

WEATHER_KEY = os.getenv("WEATHER_API_KEY")
LOCATION = os.getenv("LOCATION", "Ho Chi Minh,VN")

if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_API_KEY in environment variables!")

# ================== WEATHER MAPPING (16 kiểu) ==================
weather_mapping = {
    "Sunny": "Nắng nhẹ / Nắng ấm",
    "Clear": "Nắng nhẹ / Nắng ấm",
    "Hot": "Nắng gắt / Nắng nóng",
    "Dry": "Trời hanh khô",
    "Cold": "Trời lạnh",
    "Cloudy": "Trời âm u / Nhiều mây",
    "Overcast": "Che phủ hoàn toàn",
    "Light rain": "Mưa nhẹ / Mưa vừa",
    "Moderate rain": "Mưa nhẹ / Mưa vừa",
    "Heavy rain": "Mưa to / Mưa lớn",
    "Torrential rain": "Mưa rất to / Kéo dài",
    "Showers": "Mưa rào",
    "Thundery": "Mưa rào kèm dông / Mưa dông",
    "Thunderstorm": "Dông / Sấm sét",
    "Strong wind": "Gió giật mạnh",
    "Cyclone": "Áp thấp nhiệt đới / Bão / Siêu bão",
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
            "location": data["location"]["name"],
            "crop": "Rau muống",
        }

        # 4–7 giờ tới
        for i, hour in enumerate(data["forecast"]["forecastday"][0]["hour"][:7]):
            telemetry[f"hour_{i}_temperature"] = hour["temp_c"]
            telemetry[f"hour_{i}_humidity"] = hour["humidity"]
            cond_en = hour["condition"]["text"]
            telemetry[f"hour_{i}_weather_desc_en"] = cond_en
            telemetry[f"hour_{i}_weather_desc"] = translate_condition(cond_en)

        # Hôm nay
        today = data["forecast"]["forecastday"][0]["day"]
        telemetry.update({
            "weather_today_desc_en": today["condition"]["text"],
            "weather_today_desc": translate_condition(today["condition"]["text"]),
            "weather_today_min": today["mintemp_c"],
            "weather_today_max": today["maxtemp_c"],
            "humidity_today": today["avghumidity"],
        })

        # Ngày mai
        tomorrow = data["forecast"]["forecastday"][1]["day"]
        telemetry.update({
            "weather_tomorrow_desc_en": tomorrow["condition"]["text"],
            "weather_tomorrow_desc": translate_condition(tomorrow["condition"]["text"]),
            "weather_tomorrow_min": tomorrow["mintemp_c"],
            "weather_tomorrow_max": tomorrow["maxtemp_c"],
            "humidity_tomorrow": tomorrow["avghumidity"],
        })

        # Hôm qua (nếu có, else bỏ trống)
        telemetry.update({
            "weather_yesterday_desc_en": None,
            "weather_yesterday_desc": None,
            "weather_yesterday_min": None,
            "weather_yesterday_max": None,
            "humidity_yesterday": None,
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
