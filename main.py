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

LAT = os.getenv("LAT", "10.79")    # An Phú / Hồ Chí Minh
LON = os.getenv("LON", "106.70")
CROP = os.getenv("CROP", "Rau muống")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang",
    1: "Trời quang nhẹ",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá mạnh"
}

def get_weather_data():
    """Lấy weather cho hôm qua, hôm nay và ngày mai"""
    result = {
        "weather_yesterday_desc": "?",
        "weather_yesterday_max": 0,
        "weather_yesterday_min": 0,
        "humidity_yesterday": 0,
        "weather_today_desc": "?",
        "weather_today_max": 0,
        "weather_today_min": 0,
        "humidity_today": 0,
        "weather_tomorrow_desc": "?",
        "weather_tomorrow_max": 0,
        "weather_tomorrow_min": 0,
        "humidity_tomorrow": 0,
    }
    try:
        # --- Hôm qua ---
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        hist_url = "https://archive-api.open-meteo.com/v1/archive"
        hist_params = {
            "latitude": LAT,
            "longitude": LON,
            "start_date": yesterday,
            "end_date": yesterday,
            "hourly": "temperature_2m,relativehumidity_2m,weathercode",
            "timezone": "Asia/Ho_Chi_Minh"
        }
        r = requests.get(hist_url, params=hist_params, timeout=10)
        r.raise_for_status()
        hdata = r.json()
        temps = hdata.get("hourly", {}).get("temperature_2m", [])
        humis = hdata.get("hourly", {}).get("relativehumidity_2m", [])
        codes = hdata.get("hourly", {}).get("weathercode", [])
        if temps:
            result["weather_yesterday_max"] = max(temps)
            result["weather_yesterday_min"] = min(temps)
        if humis:
            result["humidity_yesterday"] = round(sum(humis)/len(humis),1)
        if codes:
            code_counts = {}
            for c in codes: code_counts[c] = code_counts.get(c,0)+1
            most_common = max(code_counts, key=code_counts.get)
            result["weather_yesterday_desc"] = WEATHER_CODE_MAP.get(most_common,"?")

        # --- Hôm nay & ngày mai ---
        forecast_url = "https://api.open-meteo.com/v1/forecast"
        forecast_params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "temperature_2m_max,temperature_2m_min,relativehumidity_2m_max,relativehumidity_2m_min,weathercode",
            "timezone": "Asia/Ho_Chi_Minh"
        }
        r2 = requests.get(forecast_url, params=forecast_params, timeout=10)
        r2.raise_for_status()
        fdata = r2.json().get("daily", {})
        for idx, day_key in enumerate(["today","tomorrow"]):
            if fdata:
                result[f"weather_{day_key}_desc"] = WEATHER_CODE_MAP.get(fdata["weathercode"][idx],"?")
                result[f"weather_{day_key}_max"] = fdata["temperature_2m_max"][idx]
                result[f"weather_{day_key}_min"] = fdata["temperature_2m_min"][idx]
                result[f"humidity_{day_key}"] = round((fdata["relativehumidity_2m_max"][idx]+fdata["relativehumidity_2m_min"][idx])/2,1)
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
    return result

# ================== AI HELPER ==================
def call_ai_api(data: dict) -> dict:
    """Gọi AI và/hoặc local rule"""
    weather_info = get_weather_data()
    weather_text = f"Hôm nay: {weather_info.get('weather_today_desc','?')}, {weather_info.get('weather_today_min','?')}–{weather_info.get('weather_today_max','?')}°C. Ngày mai: {weather_info.get('weather_tomorrow_desc','?')}, {weather_info.get('weather_tomorrow_min','?')}–{weather_info.get('weather_tomorrow_max','?')}°C."
    prompt = f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%. Cây: {CROP} tại An Phú, Hồ Chí Minh. {weather_text} Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."

    # Local fallback
    def local_sections(temp: float, humi: float) -> dict:
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
        care = []
        if temp >= 35:
            care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
        elif temp >= 30:
            care.append("Tưới đủ nước, theo dõi thường xuyên")
        elif temp <= 15:
            care.append("Giữ ấm, tránh sương muối")
        else:
            care.append("Nhiệt độ bình thường")
        if humi <= 40:
            care.append("Độ ẩm thấp: tăng tưới")
        elif humi <= 60:
            care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85:
            care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else:
            care.append("Độ ẩm ổn định cho rau muống")
        return {
            "prediction": pred,
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": " | ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế"
        }

    sec = local_sections(data['temperature'], data['humidity'])
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
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    weather_info = get_weather_data()
    ai_result = call_ai_api(data.dict())
    merged = {
        "location": "An Phú, Hồ Chí Minh",
        "crop": CROP,
        **data.dict(),
        **ai_result,
        **weather_info
    }
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            weather_info = get_weather_data()
            ai_result = call_ai_api(sample)
            merged = {
                "location": "An Phú, Hồ Chí Minh",
                "crop": CROP,
                **sample,
                **ai_result,
                **weather_info
            }
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)

threading.Thread(target=auto_loop, daemon=True).start()
