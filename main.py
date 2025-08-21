import os
import time
import json
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import threading
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")  # Device DEMO token
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

OWM_API_KEY = os.getenv("OWM_API_KEY", "")
LAT = float(os.getenv("LAT", "10.79"))    
LON = float(os.getenv("LON", "106.70"))

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
WEATHER_CODE_MAP = {
    "Clear": "Trời quang",
    "Clouds": "Có mây",
    "Rain": "Mưa",
    "Drizzle": "Mưa phùn",
    "Thunderstorm": "Giông",
    "Snow": "Tuyết",
    "Mist": "Sương mù",
    "Fog": "Sương mù",
    "Haze": "Sương mù"
}

def get_weather_forecast_owm():
    """Lấy dự báo hôm qua, hôm nay, ngày mai từ OpenWeatherMap"""
    if not OWM_API_KEY:
        logger.warning("OWM_API_KEY chưa cấu hình")
        return {}
    try:
        url = "https://api.openweathermap.org/data/2.5/onecall"
        params = {
            "lat": LAT,
            "lon": LON,
            "exclude": "minutely,current,alerts",
            "units": "metric",
            "lang": "vi",
            "appid": OWM_API_KEY
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        daily = data.get("daily", [])
        hourly = data.get("hourly", [])

        def mean(lst):
            return round(sum(lst)/len(lst), 1) if lst else 0

        def map_weather(w):
            if isinstance(w, list) and w:
                return WEATHER_CODE_MAP.get(w[0].get("main","?"), "?")
            return "?"

        weather_yesterday = {
            "weather_yesterday_desc": map_weather(daily[-1]["weather"]) if daily else "?",
            "weather_yesterday_max": daily[-1]["temp"]["max"] if daily else 0,
            "weather_yesterday_min": daily[-1]["temp"]["min"] if daily else 0,
            "humidity_yesterday": mean([h.get("humidity",0) for h in hourly[:24]])
        }

        weather_today = {
            "weather_today_desc": map_weather(daily[0]["weather"]) if daily else "?",
            "weather_today_max": daily[0]["temp"]["max"] if daily else 0,
            "weather_today_min": daily[0]["temp"]["min"] if daily else 0,
            "humidity_today": mean([h.get("humidity",0) for h in hourly[24:48]])
        }

        weather_tomorrow = {
            "weather_tomorrow_desc": map_weather(daily[1]["weather"]) if len(daily)>1 else "?",
            "weather_tomorrow_max": daily[1]["temp"]["max"] if len(daily)>1 else 0,
            "weather_tomorrow_min": daily[1]["temp"]["min"] if len(daily)>1 else 0,
            "humidity_tomorrow": mean([h.get("humidity",0) for h in hourly[48:72]])
        }

        return {**weather_yesterday, **weather_today, **weather_tomorrow}
    except Exception as e:
        logger.warning(f"OpenWeatherMap API error: {e}")
        return {}

# ================== AI HELPER ==================
def call_ai_api(data: dict):
    model_url = AI_API_URL
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    prompt = (
        f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%, "
        f"Cây: Rau muống tại An Phú, Hồ Chí Minh. "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_sections(temp, humi):
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
        care = []
        if temp >= 35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
        elif temp >= 30: care.append("Tưới đủ nước, theo dõi thường xuyên")
        elif temp <= 15: care.append("Giữ ấm, tránh sương muối")
        else: care.append("Nhiệt độ bình thường")
        if humi <= 40: care.append("Độ ẩm thấp: tăng tưới")
        elif humi <= 60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else: care.append("Độ ẩm ổn định cho rau muống")
        return {
            "prediction": pred,
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": " | ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
            "advice": " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"])
        }

    try:
        logger.info(f"AI ▶ {prompt[:150]}...")
        r = requests.post(model_url, headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out: 
                text = out[0].get("generated_text","") if isinstance(out[0],dict) else str(out[0])
            sec = local_sections(data['temperature'], data['humidity'])
            sec['advice'] = text.strip() or sec['advice']
            return sec
    except Exception as e:
        logger.warning(f"AI API call failed: {e}, fallback local")

    return local_sections(data['temperature'], data['humidity'])

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    for _ in range(3):
        try:
            logger.info(f"TB ▶ {data}")
            r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
            logger.info(f"TB ◀ {r.status_code} {r.text}")
            if r.status_code == 200:
                return True
        except Exception as e:
            logger.error(f"ThingsBoard push error: {e}")
        time.sleep(2)
    logger.error("Failed to push telemetry after 3 attempts")
    return False

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    weather_info = get_weather_forecast_owm()
    merged = data.dict() | ai_result | weather_info | {"location":"An Phú, Hồ Chí Minh","crop":"Rau muống"}
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2}
            ai_result = call_ai_api(sample)
            weather_info = get_weather_forecast_owm()
            merged = sample | ai_result | weather_info | {"location":"An Phú, Hồ Chí Minh","crop":"Rau muống"}
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_loop, daemon=True).start()
