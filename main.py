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
CROP_NAME = "Rau muống"

if not TB_TOKEN:
    raise RuntimeError("⚠️ Missing TB_TOKEN in environment variables!")
if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_API_KEY in environment variables!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")
logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

# ================== APP ==================
app = FastAPI()

# ================== 16 Weather Types ==================
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
    "Patchy light rain": "Có mưa cục bộ",
    "Patchy rain nearby": "Có mưa cục bộ",
    "Patchy light rain with thunder": "Mưa rào kèm dông / Mưa dông",
    "Moderate or heavy rain with thunder": "Mưa rào kèm dông / Mưa dông",
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

        telemetry = {"time": datetime.utcnow().isoformat(), "crop": CROP_NAME, "location": LOCATION}

        # --- 4–7 giờ tới (hourly forecast) ---
        hourly = data["forecast"]["forecastday"][0]["hour"][:7]  # giờ 0–6
        for i, h in enumerate(hourly[4:8]):  # giờ 4–7
            telemetry[f"hour_{i}_temperature"] = h["temp_c"]
            telemetry[f"hour_{i}_humidity"] = h["humidity"]
            telemetry[f"hour_{i}_weather_desc"] = translate_condition(h["condition"]["text"])
            telemetry[f"hour_{i}_weather_desc_en"] = h["condition"]["text"]

        # --- Hôm qua / hôm nay / ngày mai ---
        days = [data["forecast"]["forecastday"][0]]  # hôm nay
        if len(data["forecast"]["forecastday"]) > 1:
            days.append(data["forecast"]["forecastday"][1])  # ngày mai

        # Nếu API hỗ trợ yesterday, có thể thêm ngày hôm qua (tạm bỏ nếu API free ko có)
        for idx, d in enumerate(days):
            day_key = ["today", "tomorrow"][idx]
            telemetry[f"weather_{day_key}_desc"] = translate_condition(d["day"]["condition"]["text"])
            telemetry[f"weather_{day_key}_desc_en"] = d["day"]["condition"]["text"]
            telemetry[f"weather_{day_key}_max"] = d["day"]["maxtemp_c"]
            telemetry[f"weather_{day_key}_min"] = d["day"]["mintemp_c"]
            telemetry[f"humidity_{day_key}"] = d["day"]["avghumidity"]

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
