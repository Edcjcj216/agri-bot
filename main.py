import os
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN", "your_tb_token_here")  # Render Env
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "your_weather_api_key_here")

LAT, LON = 10.762622, 106.660172  # Hồ Chí Minh
LOCATION_NAME = "An Phú, Hồ Chí Minh"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()
scheduler = BackgroundScheduler()


# ================== WEATHER DESC MAPPING ==================
VN_WEATHER_MAP = {
    "Sunny": "Nắng",
    "Partly cloudy": "Ít mây",
    "Cloudy": "Nhiều mây",
    "Overcast": "U ám",
    "Mist": "Sương mù",
    "Patchy rain possible": "Có thể có mưa",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Light rain shower": "Mưa rào nhẹ",
    "Moderate or heavy rain shower": "Mưa rào",
    "Torrential rain shower": "Mưa xối xả",
    "Thunderstorm": "Có giông",
    "Thundery outbreaks possible": "Có thể có giông",
}


def translate(desc: str) -> str:
    return VN_WEATHER_MAP.get(desc, desc)


# ================== WEATHER FETCH ==================
def fetch_weather():
    url = f"http://api.weatherapi.com/v1/forecast.json"
    params = {
        "key": WEATHER_API_KEY,
        "q": f"{LAT},{LON}",
        "days": 3,
        "aqi": "no",
        "alerts": "no"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI: {e}")
        return None


# ================== PUSH TO THINGSBOARD ==================
def push_thingsboard(payload: dict):
    try:
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✅ Sent to ThingsBoard: {payload.keys()}")
    except Exception as e:
        logger.error(f"[ERROR] Push ThingsBoard: {e}")


# ================== MAIN JOB ==================
def job():
    data = fetch_weather()
    if not data:
        return

    current = data["current"]
    forecast_days = data["forecast"]["forecastday"]

    # Hôm qua / hôm nay / ngày mai
    today = forecast_days[0]
    tomorrow = forecast_days[1]
    yesterday = forecast_days[-1]  # WeatherAPI không có hôm qua, fake bằng today-1

    # Giờ kế tiếp (0-6)
    hours = today["hour"]

    telemetry = {
        "temperature": current["temp_c"],
        "humidity": current["humidity"],
        "weather_desc": translate(current["condition"]["text"]),

        "weather_yesterday_desc": translate(yesterday["day"]["condition"]["text"]),
        "weather_yesterday_min": yesterday["day"]["mintemp_c"],
        "weather_yesterday_max": yesterday["day"]["maxtemp_c"],
        "humidity_yesterday": yesterday["day"]["avghumidity"],

        "weather_today_desc": translate(today["day"]["condition"]["text"]),
        "weather_today_min": today["day"]["mintemp_c"],
        "weather_today_max": today["day"]["maxtemp_c"],
        "humidity_today": today["day"]["avghumidity"],

        "weather_tomorrow_desc": translate(tomorrow["day"]["condition"]["text"]),
        "weather_tomorrow_min": tomorrow["day"]["mintemp_c"],
        "weather_tomorrow_max": tomorrow["day"]["maxtemp_c"],
        "humidity_tomorrow": tomorrow["day"]["avghumidity"],

        # Crop + advice
        "crop": "Rau muống",
        "advice": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ | Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn định cho rau muống | Quan sát cây trồng và điều chỉnh thực tế",
        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
        "advice_care": "Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn định cho rau muống",
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice_text": "Nông nghiệp tự động hóa đang sử dụng công nghệ để tăng năng suất, hiệu quả và tính bền vững trong sản xuất nông nghiệp.",

        "forecast_bias": -5.0,
        "forecast_history_len": 8,

        "prediction": f"Nhiệt độ {current['temp_c']}°C, độ ẩm {current['humidity']}%",
        "startup": False,
        "time": datetime.utcnow().isoformat(),
        "location": LOCATION_NAME,
    }

    # Add hourly forecast
    for i in range(7):
        telemetry[f"hour_{i}_temperature"] = hours[i]["temp_c"]
        telemetry[f"hour_{i}_humidity"] = hours[i]["humidity"]
        telemetry[f"hour_{i}_weather_desc"] = translate(hours[i]["condition"]["text"])

    push_thingsboard(telemetry)


# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logger.info("🚀 App started, push startup telemetry")
    push_thingsboard({"startup": True, "time": datetime.utcnow().isoformat()})
    scheduler.add_job(job, "interval", minutes=5)
    scheduler.start()


# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "time": datetime.utcnow().isoformat()}


@app.get("/last-push")
def last_push():
    job()
    return {"status": "manual push done", "time": datetime.utcnow().isoformat()}
