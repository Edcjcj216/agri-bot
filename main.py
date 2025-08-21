import os
import time
import json
import logging
import requests
import threading
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
import random

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "I1s5bI2FQCZw6umLvwLG")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

OWM_API_KEY = os.getenv("OWM_API_KEY", "")  # OpenWeatherMap API
LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

AUTO_LOOP_INTERVAL = 300  # 5 phút

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
    """Lấy dự báo hôm qua, hôm nay, ngày mai từ OpenWeatherMap"""
    try:
        if not OWM_API_KEY:
            logger.warning("OWM_API_KEY chưa cấu hình")
            return _empty_weather()
        
        # Lấy dữ liệu forecast 3 ngày
        url = "https://api.openweathermap.org/data/2.5/onecall"
        params = {
            "lat": LAT,
            "lon": LON,
            "exclude": "minutely,hourly,alerts",
            "appid": OWM_API_KEY,
            "units": "metric",
            "lang": "vi"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        daily = data.get("daily", [])
        # đảm bảo ít nhất 3 ngày: yesterday, today, tomorrow
        if len(daily) < 3:
            return _empty_weather()
        
        def map_desc(desc_en):
            return OWM_DESC_VI.get(desc_en.lower(), desc_en)
        
        weather_yesterday = {
            "weather_yesterday_desc": map_desc(daily[0]["weather"][0]["description"]),
            "weather_yesterday_max": daily[0]["temp"]["max"],
            "weather_yesterday_min": daily[0]["temp"]["min"],
            "humidity_yesterday": daily[0]["humidity"]
        }
        weather_today = {
            "weather_today_desc": map_desc(daily[1]["weather"][0]["description"]),
            "weather_today_max": daily[1]["temp"]["max"],
            "weather_today_min": daily[1]["temp"]["min"],
            "humidity_today": daily[1]["humidity"]
        }
        weather_tomorrow = {
            "weather_tomorrow_desc": map_desc(daily[2]["weather"][0]["description"]),
            "weather_tomorrow_max": daily[2]["temp"]["max"],
            "weather_tomorrow_min": daily[2]["temp"]["min"],
            "humidity_tomorrow": daily[2]["humidity"]
        }
        return {**weather_yesterday, **weather_today, **weather_tomorrow}
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return _empty_weather()

def _empty_weather():
    keys = ["weather_yesterday_desc","weather_yesterday_max","weather_yesterday_min","humidity_yesterday",
            "weather_today_desc","weather_today_max","weather_today_min","humidity_today",
            "weather_tomorrow_desc","weather_tomorrow_max","weather_tomorrow_min","humidity_tomorrow"]
    return {k: 0 if "max" in k or "min" in k or "humidity" in k else "?" for k in keys}

# ================== AI ADVICE ==================
def call_ai_advice(temp, hum, weather_desc):
    """Gọi AI (HuggingFace) hoặc fallback local"""
    prompt = f"Nhiệt độ {temp}°C, độ ẩm {hum}%, thời tiết {weather_desc}. Viết 1 câu dự báo + 1 câu chăm sóc rau muống."
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    body = {"inputs": prompt, "options":{"wait_for_model":True}}
    
    def fallback_local():
        advice_options = []
        # Nhiệt độ
        if temp > 35: advice_options.append(random.choice(["Tránh nắng gắt", "Tưới sáng sớm/chiều mát"]))
        elif temp >= 30: advice_options.append(random.choice(["Tưới đủ nước", "Theo dõi sâu bệnh"]))
        elif temp <= 15: advice_options.append(random.choice(["Giữ ấm", "Tránh sương muối"]))
        else: advice_options.append("Nhiệt độ bình thường")
        # Độ ẩm
        if hum <= 40: advice_options.append("Độ ẩm thấp: tăng tưới")
        elif hum <= 60: advice_options.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif hum >= 85: advice_options.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else: advice_options.append("Độ ẩm ổn định cho rau muống")
        return {
            "prediction": f"Nhiệt độ {temp:.1f}°C, độ ẩm {hum:.1f}%",
            "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
            "advice_care": " | ".join(advice_options),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
            "advice": " | ".join(["Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ"] + advice_options + ["Quan sát cây trồng và điều chỉnh thực tế"])
        }
    
    try:
        r = requests.post(AI_API_URL, headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out: text = out[0].get("generated_text","") if isinstance(out[0],dict) else str(out[0])
            if text.strip():
                lines = text.strip().split(".")
                care_line = lines[1].strip() if len(lines)>1 else lines[0].strip()
                return {
                    "prediction": lines[0].strip() if len(lines)>0 else f"Nhiệt độ {temp:.1f}°C, độ ẩm {hum:.1f}%",
                    "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
                    "advice_care": care_line,
                    "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
                    "advice": " | ".join(["Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ", care_line, "Quan sát cây trồng và điều chỉnh thực tế"])
                }
    except Exception as e:
        logger.warning(f"AI API failed: {e}")
    
    return fallback_local()

# ================== PUSH THINGSBOARD ==================
def send_to_thingsboard(data):
    try:
        logger.info(f"TB ▶ {data}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status":"running","demo_token":TB_DEMO_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    weather_info = get_weather_forecast()
    ai_result = call_ai_advice(data.temperature, data.humidity, weather_info.get("weather_today_desc","?"))
    merged = {**data.dict(), **ai_result, **weather_info,
              "location":"An Phú, Hồ Chí Minh","crop":"Rau muống"}
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": random.uniform(20,35), "humidity": random.uniform(40,85)}
            weather_info = get_weather_forecast()
            ai_result = call_ai_advice(sample["temperature"], sample["humidity"], weather_info.get("weather_today_desc","?"))
            merged = {**sample, **ai_result, **weather_info,
                      "location":"An Phú, Hồ Chí Minh","crop":"Rau muống"}
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(AUTO_LOOP_INTERVAL)

threading.Thread(target=auto_loop, daemon=True).start()
