import os
import time
import json
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import threading
from datetime import datetime

# ================== CONFIG ==================
TB_DEMO_TOKEN = "I1s5bI2FQCZw6umLvwLG"  # Device DEMO token mới
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

LAT = float(os.getenv("LAT", "10.79"))    # An Phú / Hồ Chí Minh
LON = float(os.getenv("LON", "106.70"))
OW_KEY = os.getenv("OW_KEY", "a53f443795604c41b72305c1806784db")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== WEATHER (OpenWeather) ==================
def get_weather_forecast():
    """Lấy dự báo hôm nay và ngày mai từ OpenWeather (daily.humidity, temp_min/max, description)."""
    try:
        url = "https://api.openweathermap.org/data/3.0/onecall"
        params = {
            "lat": LAT,
            "lon": LON,
            "exclude": "current,minutely,hourly,alerts",
            "appid": OW_KEY,
            "units": "metric",
            "lang": "vi"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", [])

        def pick_day(idx, prefix):
            if idx < len(daily):
                d = daily[idx]
                return {
                    f"weather_{prefix}_desc": d["weather"][0]["description"] if d.get("weather") else "?",
                    f"weather_{prefix}_max": round(d["temp"]["max"],1) if "temp" in d else 0,
                    f"weather_{prefix}_min": round(d["temp"]["min"],1) if "temp" in d else 0,
                    f"humidity_{prefix}": d.get("humidity",0)
                }
            return {
                f"weather_{prefix}_desc": "?",
                f"weather_{prefix}_max": 0,
                f"weather_{prefix}_min": 0,
                f"humidity_{prefix}": 0
            }

        weather_today = pick_day(0,"today")
        weather_tomorrow = pick_day(1,"tomorrow")

        return {**weather_today, **weather_tomorrow}
    except Exception as e:
        logger.warning(f"OpenWeather API error: {e}")
        return {
            "weather_today_desc":"?", "weather_today_max":0, "weather_today_min":0, "humidity_today":0,
            "weather_tomorrow_desc":"?", "weather_tomorrow_max":0, "weather_tomorrow_min":0, "humidity_tomorrow":0
        }

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
    ai_result = call_ai_api(data.dict())
    weather_info = get_weather_forecast()
    merged = data.dict() | ai_result | weather_info | {"location":"An Phú, Hồ Chí Minh","crop":"Rau muống"}
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2}
            ai_result = call_ai_api(sample)
            weather_info = get_weather_forecast()
            merged = sample | ai_result | weather_info | {"location":"An Phú, Hồ Chí Minh","crop":"Rau muống"}
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_loop, daemon=True).start()
