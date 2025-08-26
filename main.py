import os
import json
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from fastapi.responses import JSONResponse

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

# ================== WEATHER MAPPING ==================
weather_mapping = {
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

        # 4–7 giờ tới (chúng ta sẽ lấy 4 giờ sau này)
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

        # Hôm qua (để trống)
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
    if not telemetry:
        return JSONResponse(status_code=500, content={"error": "Không thể lấy dữ liệu thời tiết"})
    
    now = datetime.utcnow()
    
    forecast_hours = []
    for i in range(4):  # chỉ 4 giờ tiếp theo
        hour_key_temp = f"hour_{i}_temperature"
        hour_key_hum = f"hour_{i}_humidity"
        hour_key_desc = f"hour_{i}_weather_desc"  # tiếng Việt
        hour_time = now + timedelta(hours=i)
        hour_display = hour_time.strftime("%H giờ")  # chỉ giờ

        forecast_hours.append({
            "hour": hour_display,
            "temperature": telemetry.get(hour_key_temp),
            "humidity": telemetry.get(hour_key_hum),
            "weather": telemetry.get(hour_key_desc),
        })
    
    result = {
        "current_hour": now.strftime("%H giờ"),
        "forecast_next_4_hours": forecast_hours,
        "today": {
            "min_temp": telemetry.get("weather_today_min"),
            "max_temp": telemetry.get("weather_today_max"),
            "humidity": telemetry.get("humidity_today"),
            "weather": telemetry.get("weather_today_desc"),
        },
        "tomorrow": {
            "min_temp": telemetry.get("weather_tomorrow_min"),
            "max_temp": telemetry.get("weather_tomorrow_max"),
            "humidity": telemetry.get("humidity_tomorrow"),
            "weather": telemetry.get("weather_tomorrow_desc"),
        },
        "yesterday": {
            "min_temp": telemetry.get("weather_yesterday_min"),
            "max_temp": telemetry.get("weather_yesterday_max"),
            "humidity": telemetry.get("humidity_yesterday"),
            "weather": telemetry.get("weather_yesterday_desc"),
        }
    }

    return JSONResponse(content=result)
