import os
import time
import json
import random
import logging
import requests
import threading
from datetime import datetime, timedelta
from fastapi import FastAPI
from pydantic import BaseModel

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "I1s5bI2FQCZw6umLvwLG")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

OWM_API_KEY = os.getenv("OWM_API_KEY", "")
OWM_LAT = float(os.getenv("LAT", "10.79"))
OWM_LON = float(os.getenv("LON", "106.70"))

AI_API_URL = os.getenv("AI_API_URL", "")
AI_TOKEN = os.getenv("AI_TOKEN", "")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== WEATHER ==================
OWM_DESC_VI = {
    "clear sky": "Trời quang",
    "few clouds": "Trời quang nhẹ",
    "scattered clouds": "Có mây",
    "broken clouds": "Nhiều mây",
    "shower rain": "Mưa rào",
    "rain": "Mưa",
    "light rain": "Mưa nhẹ",
    "moderate rain": "Mưa vừa",
    "heavy intensity rain": "Mưa to",
    "thunderstorm": "Giông",
    "snow": "Tuyết",
    "mist": "Sương mù"
}

def get_weather_forecast():
    """Lấy hôm qua / hôm nay / ngày mai từ OpenWeatherMap"""
    if not OWM_API_KEY:
        logger.warning("OWM_API_KEY chưa cấu hình")
        return {}

    try:
        url = "https://api.openweathermap.org/data/2.5/onecall"
        params = {
            "lat": OWM_LAT,
            "lon": OWM_LON,
            "exclude": "minutely,alerts",
            "units": "metric",
            "appid": OWM_API_KEY
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        def format_day(daily, index):
            day = daily[index]
            weather = day['weather'][0]['description']
            return {
                f"weather_day{index}_desc": OWM_DESC_VI.get(weather, weather),
                f"weather_day{index}_max": day['temp']['max'],
                f"weather_day{index}_min": day['temp']['min'],
                f"humidity_day{index}": day.get('humidity', 0)
            }

        weather_info = {}
        for i in range(3):  # hôm qua, hôm nay, ngày mai
            weather_info.update(format_day(data['daily'], i))
        return weather_info
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {}

# ================== AI HELPER ==================
def call_ai_api(temp, humi):
    """Gọi AI API thật, fallback local"""
    prompt = f"Nhiệt độ {temp:.1f}°C, độ ẩm {humi:.1f}%, cây rau muống tại An Phú, Hồ Chí Minh. Viết dự đoán ngắn gọn + gợi ý chăm sóc."

    def fallback_local(temp, humi):
        adv = []
        if temp >= 35: adv.append(random.choice(["Tránh nắng gắt", "Tưới sáng sớm/chiều mát"]))
        elif temp >= 30: adv.append(random.choice(["Tưới đủ nước", "Theo dõi sâu bệnh"]))
        elif temp <= 15: adv.append(random.choice(["Giữ ấm", "Tránh sương muối"]))
        else: adv.append("Nhiệt độ bình thường")

        if humi <= 40: adv.append("Độ ẩm thấp: tăng tưới")
        elif humi <= 60: adv.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85: adv.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else: adv.append("Độ ẩm ổn định cho rau muống")

        return {
            "prediction": f"Nhiệt độ {temp:.1f}°C, độ ẩm {humi:.1f}%",
            "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
            "advice_care": " | ".join(adv),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
            "advice": " | ".join(["Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ"] + adv + ["Quan sát cây trồng và điều chỉnh thực tế"])
        }

    if not AI_API_URL:
        return fallback_local(temp, humi)

    try:
        headers = {"Authorization": f"Bearer {AI_TOKEN}"} if AI_TOKEN else {}
        body = {"inputs": prompt, "options": {"wait_for_model": True}}
        r = requests.post(AI_API_URL, headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out:
                text = out[0].get("generated_text","") if isinstance(out[0], dict) else str(out[0])
            sec = fallback_local(temp, humi)
            sec['advice'] = text.strip() or sec['advice']
            return sec
    except Exception as e:
        logger.warning(f"AI API call failed: {e}, fallback local")
    return fallback_local(temp, humi)

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {data}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.temperature, data.humidity)
    weather_info = get_weather_forecast()
    merged = data.dict() | ai_result | weather_info | {"location": "An Phú, Hồ Chí Minh", "crop": "Rau muống"}
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            # tạo sample khi ESP32 chưa gửi
            sample = {"temperature": random.uniform(18,32), "humidity": random.uniform(40,85)}
            ai_result = call_ai_api(sample["temperature"], sample["humidity"])
            weather_info = get_weather_forecast()
            merged = sample | ai_result | weather_info | {"location": "An Phú, Hồ Chí Minh", "crop": "Rau muống"}
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_loop, daemon=True).start()
