import os
import time
import json
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import threading

# ================== CONFIG ==================
TB_DEMO_TOKEN = "pk94asonfacs6mbeuutg"  # Device DEMO token
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "a53f443795604c41b72305c1806784db")
WEATHER_CITY = os.getenv("WEATHER_CITY", "Ho Chi Minh")

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
def get_weather(city: str = WEATHER_CITY) -> dict:
    """Lấy dự báo thời tiết hiện tại"""
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather"
        params = {"q": city, "appid": WEATHER_API_KEY, "units": "metric", "lang": "vi"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {
            "weather_now_temp": data["main"]["temp"],
            "weather_now_humidity": data["main"]["humidity"],
            "weather_now_desc": data["weather"][0]["description"]
        }
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {}

def get_weather_24h(city: str = WEATHER_CITY) -> dict:
    """Lấy dự báo 24h (1 tiếng/lần)"""
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast"
        params = {"q": city, "appid": WEATHER_API_KEY, "units": "metric", "cnt": 24, "lang": "vi"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        forecasts = {}
        for i, entry in enumerate(data["list"][:24]):
            forecasts[f"hour_{i+1}_temp"] = entry["main"]["temp"]
            forecasts[f"hour_{i+1}_humidity"] = entry["main"]["humidity"]
            forecasts[f"hour_{i+1}_desc"] = entry["weather"][0]["description"]
        return forecasts
    except Exception as e:
        logger.warning(f"Weather 24h API error: {e}")
        return {}

def get_weather_tomorrow(city: str = WEATHER_CITY) -> dict:
    """Dự báo tổng quát cho ngày mai"""
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast"
        params = {"q": city, "appid": WEATHER_API_KEY, "units": "metric", "cnt": 16, "lang": "vi"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        tomorrow = data["list"][8]  # 24h sau (3h * 8)
        return {
            "tomorrow_temp": tomorrow["main"]["temp"],
            "tomorrow_humidity": tomorrow["main"]["humidity"],
            "tomorrow_desc": tomorrow["weather"][0]["description"]
        }
    except Exception as e:
        logger.warning(f"Weather tomorrow API error: {e}")
        return {}

# ================== AI HELPER ==================
def call_ai_api(data: dict) -> dict:
    """Gọi AI + local rule"""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    weather_info = get_weather()
    weather_text = ""
    if weather_info:
        weather_text = f" Thời tiết: {weather_info['weather_now_desc']}, {weather_info['weather_now_temp']}°C, {weather_info['weather_now_humidity']}%RH."

    prompt = (
        f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, "
        f"độ ẩm {data['humidity']}%, pin {data.get('battery','?')}%. "
        f"Cây: Rau muống tại Hồ Chí Minh.{weather_text} "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_sections(temp: float, humi: float, battery: float | None = None) -> dict:
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        care = []
        if temp >= 35:
            care.append("- Tránh nắng gắt, tưới sáng sớm/chiều mát")
        elif temp >= 30:
            care.append("- Tưới đủ nước, theo dõi thường xuyên")
        elif temp <= 15:
            care.append("- Giữ ấm, tránh sương muối")
        else:
            care.append("- Nhiệt độ bình thường")
        if humi <= 40:
            care.append("- Độ ẩm thấp: tăng tưới")
        elif humi <= 60:
            care.append("- Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85:
            care.append("- Độ ẩm cao: tránh úng")
        else:
            care.append("- Độ ẩm ổn định cho rau muống")
        if battery is not None and battery <= 20:
            care.append("- Pin thấp: kiểm tra nguồn")
        return {
            "prediction": pred,
            "advice": " ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế"
        }

    try:
        r = requests.post(AI_API_URL, headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            out = r.json()
            text = out[0].get("generated_text") if isinstance(out, list) and out else str(out)
            local = local_sections(data['temperature'], data['humidity'], data.get('battery'))
            return local | {"advice_ai": text.strip()}
    except Exception as e:
        logger.warning(f"AI API call failed: {e}, fallback local")

    return local_sections(data['temperature'], data['humidity'], data.get('battery'))

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
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    merged = data.dict() | ai_result | get_weather() | get_weather_tomorrow()
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

@app.get("/weather")
def weather_api():
    return get_weather() | get_weather_24h() | get_weather_tomorrow()

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2, "battery": 90}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            ai_result = call_ai_api(sample)
            merged = sample | ai_result | get_weather() | get_weather_24h() | get_weather_tomorrow()
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(3600)  # mỗi giờ chạy 1 lần

threading.Thread(target=auto_loop, daemon=True).start()
