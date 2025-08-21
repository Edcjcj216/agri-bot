import os
import time
import logging
import random
import requests
import threading
from fastapi import FastAPI
from pydantic import BaseModel

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "I1s5bI2FQCZw6umLvwLG")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

OWM_API_KEY = os.getenv("OWM_API_KEY", "")
LAT = float(os.getenv("LAT", "10.79"))    # An Phú / Hồ Chí Minh
LON = float(os.getenv("LON", "106.70"))

AI_API_URL = os.getenv("AI_API_URL", "")  # HuggingFace / OpenAI endpoint
AI_TOKEN = os.getenv("AI_TOKEN", "")      # API key cho AI

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
    if not OWM_API_KEY:
        logger.warning("OWM_API_KEY chưa cấu hình")
        return {}
    try:
        url = "https://api.openweathermap.org/data/2.5/onecall"
        params = {
            "lat": LAT,
            "lon": LON,
            "exclude": "minutely,hourly,alerts",
            "units": "metric",
            "appid": OWM_API_KEY,
            "lang": "vi"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        today = data["daily"][0]
        tomorrow = data["daily"][1]

        def vi_desc(weather):
            d = weather[0]["description"]
            return OWM_DESC_VI.get(d.lower(), d.capitalize())

        return {
            "weather_today_desc": vi_desc(today["weather"]),
            "weather_today_max": today["temp"]["max"],
            "weather_today_min": today["temp"]["min"],
            "weather_tomorrow_desc": vi_desc(tomorrow["weather"]),
            "weather_tomorrow_max": tomorrow["temp"]["max"],
            "weather_tomorrow_min": tomorrow["temp"]["min"]
        }
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {}

# ================== AI ==================
def call_ai_api(temp, humi):
    """Gọi HuggingFace / OpenAI API để sinh advice"""
    prompt = (
        f"Cảm biến: nhiệt độ {temp}°C, độ ẩm {humi}%, "
        f"Cây: Rau muống tại An Phú, Hồ Chí Minh. "
        "Viết 1 câu dự đoán tình trạng cây trồng và 1 câu gợi ý chăm sóc ngắn gọn, tự nhiên."
    )
    headers = {"Authorization": f"Bearer {AI_TOKEN}"} if AI_TOKEN else {}
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    try:
        if AI_API_URL and AI_TOKEN:
            r = requests.post(AI_API_URL, headers=headers, json=body, timeout=20)
            if r.status_code == 200:
                out = r.json()
                text = ""
                if isinstance(out, list) and out:
                    text = out[0].get("generated_text","") if isinstance(out[0], dict) else str(out[0])
                if text.strip():
                    return {
                        "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%",
                        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
                        "advice_care": text.strip(),
                        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
                        "advice": text.strip()
                    }
    except Exception as e:
        logger.warning(f"AI API call failed: {e}")

    # fallback local nếu API fail
    return local_ai_fallback(temp, humi)

def local_ai_fallback(temp, humi):
    """Fallback AI logic, linh hoạt, không rập khuôn"""
    temp_phrases = []
    if temp > 35:
        temp_phrases = [
            "Tránh nắng gắt, tưới sáng sớm hoặc chiều mát",
            "Che nắng cho cây, theo dõi lá héo",
            "Theo dõi cây thường xuyên do nhiệt độ cao"
        ]
    elif temp >= 30:
        temp_phrases = [
            "Tưới nước đủ, giám sát sâu bệnh",
            "Theo dõi lá và tưới khi cần",
            "Nhiệt độ cao, chăm sóc cây kỹ lưỡng"
        ]
    elif temp <= 15:
        temp_phrases = [
            "Giữ ấm, tránh sương muối",
            "Che phủ cây để tránh lạnh",
            "Theo dõi cây do nhiệt độ thấp"
        ]
    else:
        temp_phrases = [
            "Nhiệt độ bình thường, cây phát triển ổn định",
            "Điều kiện thuận lợi, theo dõi định kỳ"
        ]

    humi_phrases = []
    if humi <= 40:
        humi_phrases = ["Độ ẩm thấp: tăng tưới", "Cây cần nước, tưới thêm", "Theo dõi lá khô"]
    elif humi <= 60:
        humi_phrases = ["Độ ẩm hơi thấp, tưới khi cần", "Cây ổn định, theo dõi định kỳ"]
    elif humi >= 85:
        humi_phrases = ["Độ ẩm cao: tránh úng, kiểm tra thoát nước", "Kiểm tra rễ và đất"]
    else:
        humi_phrases = ["Độ ẩm ổn định, cây phát triển tốt"]

    advice_parts = random.sample(temp_phrases, 1) + random.sample(humi_phrases, 1)
    random.shuffle(advice_parts)
    advice = " | ".join(advice_parts + ["Quan sát cây trồng và điều chỉnh thực tế"])

    return {
        "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%",
        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
        "advice_care": " | ".join(advice_parts),
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": advice
    }

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

# ================== AUTO LOOP (TEST SAMPLE) ==================
def auto_loop():
    while True:
        try:
            sample = {
                "temperature": round(random.uniform(20, 35), 1),
                "humidity": round(random.uniform(40, 85), 1)
            }
            ai_result = call_ai_api(sample["temperature"], sample["humidity"])
            weather_info = get_weather_forecast()
            merged = sample | ai_result | weather_info | {"location": "An Phú, Hồ Chí Minh", "crop": "Rau muống"}
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_loop, daemon=True).start()
