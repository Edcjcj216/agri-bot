import os
import time
import json
import logging
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import threading

# ================== CONFIG ==================
TB_DEMO_TOKEN = "pk94asonfacs6mbeuutg"  # Device DEMO token
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

TB_TENANT_USER = os.getenv("TB_TENANT_USER", "")
TB_TENANT_PASS = os.getenv("TB_TENANT_PASS", "")

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
    """Lấy dự báo thời tiết hiện tại từ OpenWeatherMap"""
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather"
        params = {"q": city, "appid": WEATHER_API_KEY, "units": "metric", "lang": "vi"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {
            "weather_temp": data["main"]["temp"],
            "weather_humidity": data["main"]["humidity"],
            "weather_desc": data["weather"][0]["description"]
        }
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {}

# ================== AI HELPER ==================
def call_ai_api(data: dict) -> dict:
    """Gọi AI và/hoặc local rule"""
    model_url = AI_API_URL
    hf_token = HF_TOKEN
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}

    weather_info = get_weather()
    weather_text = ""
    if weather_info:
        weather_text = f" Dự báo thời tiết: {weather_info['weather_desc']}, {weather_info['weather_temp']}°C, {weather_info['weather_humidity']}%RH."

    prompt = (
        f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%, pin {data.get('battery','?')}%. "
        f"Cây: Rau muống tại Hồ Chí Minh.{weather_text} "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_sections(temp: float, humi: float, battery: float | None = None) -> dict:
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        nutrition = [
            "**Dinh dưỡng:** - Ưu tiên Kali (K)",
            "- Cân bằng NPK",
            "- Bón phân hữu cơ",
            "- Phân bón lá nếu cần",
        ]
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
            care.append("- Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else:
            care.append("- Độ ẩm ổn định cho rau muống")
        if battery is not None and battery <= 20:
            care.append("- Pin thấp: kiểm tra nguồn")
        note = "**Lưu ý:** Quan sát cây trồng và điều chỉnh thực tế"
        return {
            "prediction": pred,
            "advice_nutrition": " ".join(nutrition),
            "advice_care": " ".join(care),
            "advice_note": note,
        }

    try:
        logger.info(f"AI ▶ {model_url} body={prompt[:200]}")
        r = requests.post(model_url, headers=headers, json=body, timeout=30)
        logger.info(f"AI ◀ status={r.status_code}")
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out:
                first = out[0]
                if isinstance(first, dict):
                    text = first.get("generated_text") or first.get("text") or str(first)
                else:
                    text = str(first)
            elif isinstance(out, dict):
                text = out.get("generated_text") or out.get("text") or json.dumps(out, ensure_ascii=False)
            else:
                text = str(out)

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
    sec['advice'] = f"{sec['advice_nutrition']} {sec['advice_care']} {sec['advice_note']}"
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
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    merged = data.dict() | ai_result
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

@app.get("/weather")
def get_weather_api():
    return get_weather()

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2, "battery": 90}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            ai_result = call_ai_api(sample)
            merged = sample | ai_result
            merged.update(get_weather())  # gắn thêm dữ liệu thời tiết
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)

threading.Thread(target=auto_loop, daemon=True).start()
