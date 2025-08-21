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
TB_DEMO_TOKEN = "sgkxcrqntuki8gu1oj8u"  # Device DEMO token
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

LOCATION_NAME = "An Phú, Hồ Chí Minh"
CROP = "Rau muống"

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
    0: "Trời quang", 1: "Trời quang nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Giông nhẹ hoặc vừa", 96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

LAT = "10.79"    # chỉ dùng cho API forecast
LON = "106.70"

def get_weather_forecast() -> dict:
    """Lấy dự báo thời tiết hôm nay và ngày mai từ Open-Meteo"""
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Ho_Chi_Minh"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        if not daily:
            return {}
        weather_today = {
            "weather_desc": WEATHER_CODE_MAP.get(daily["weathercode"][0], "?"),
            "temp_max": daily["temperature_2m_max"][0],
            "temp_min": daily["temperature_2m_min"][0]
        }
        weather_tomorrow = {
            "weather_desc": WEATHER_CODE_MAP.get(daily["weathercode"][1], "?"),
            "temp_max": daily["temperature_2m_max"][1],
            "temp_min": daily["temperature_2m_min"][1]
        }
        return {"weather_today": weather_today, "weather_tomorrow": weather_tomorrow}
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {}

# ================== AI HELPER ==================
def call_ai_api(data: dict) -> dict:
    """Gọi AI và/hoặc local rule"""
    model_url = AI_API_URL
    hf_token = HF_TOKEN
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}

    weather_info = get_weather_forecast()
    weather_text = ""
    if weather_info:
        hn = weather_info.get("weather_today", {})
        weather_text = f" Dự báo hôm nay: {hn.get('weather_desc','?')}, {hn.get('temp_min','?')}–{hn.get('temp_max','?')}°C."

    prompt = (
        f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%, pin {data.get('battery','?')}%. "
        f"Cây: {CROP} tại {LOCATION_NAME}.{weather_text} "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_sections(temp: float, humi: float, battery: float | None = None) -> dict:
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
        if battery is not None and battery <= 20: care.append("Pin thấp: kiểm tra nguồn")
        return {
            "prediction": pred,
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": " | ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế"
        }

    try:
        logger.info(f"AI ▶ {prompt[:150]}...")
        r = requests.post(model_url, headers=headers, json=body, timeout=30)
        logger.info(f"AI ◀ status={r.status_code}")
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out:
                first = out[0]
                if isinstance(first, dict):
                    text = first.get("generated_text") or first.get("text") or str(first)
                else: text = str(first)
            elif isinstance(out, dict):
                text = out.get("generated_text") or out.get("text") or json.dumps(out, ensure_ascii=False)
            else: text = str(out)

            sections = local_sections(data['temperature'], data['humidity'], data.get('battery'))
            return {
                "prediction": sections['prediction'],
                "advice": text.strip(),
                "advice_nutrition": sections['advice_nutrition'],
                "advice_care": sections['advice_care'],
                "advice_note": sections['advice_note'],
            }
    except Exception as e:
        logger.warning(f"AI API call failed: {e}, fallback local")

    sec = local_sections(data['temperature'], data['humidity'], data.get('battery'))
    sec['advice'] = f"{sec['advice_nutrition']} | {sec['advice_care']} | {sec['advice_note']}"
    return sec

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
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***", "location": LOCATION_NAME, "crop": CROP}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    weather_info = get_weather_forecast()
    merged = data.dict() | ai_result | weather_info | {"location": LOCATION_NAME, "crop": CROP}
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

@app.get("/weather")
def get_weather_api():
    return get_weather_forecast()

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2, "battery": 90}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            ai_result = call_ai_api(sample)
            weather_info = get_weather_forecast()
            merged = sample | ai_result | weather_info | {"location": LOCATION_NAME, "crop": CROP}
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)

threading.Thread(target=auto_loop, daemon=True).start()
