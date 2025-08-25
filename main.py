import os
import time
import json
import logging
import requests
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "your_openweather_key_here")
LAT = "10.762622"   # HCM mặc định
LON = "106.660172"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "your_openrouter_key_here")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

INTERVAL = 300  # 5 phút

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ================== UTILS ==================
def push_telemetry(data: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        logging.info(f"📤 Sending telemetry: {json.dumps(data, ensure_ascii=False)}")
        resp = requests.post(url, json=data, timeout=10)
        logging.info(f"✅ TB Response {resp.status_code}: {resp.text}")
    except Exception as e:
        logging.error(f"❌ Error pushing telemetry: {e}")

def get_openweather():
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={LAT}&lon={LON}&units=metric&appid={OPENWEATHER_API_KEY}&lang=vi"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # Giờ hiện tại
    current = data["list"][0]
    main = current["main"]
    weather_desc = current["weather"][0]["description"]

    result = {
        "temperature": main["temp"],
        "humidity": main["humidity"],
        "weather_today_desc": weather_desc,
        "location": data["city"]["name"]
    }

    # Thêm vài khung giờ kế tiếp
    for i in range(6):
        forecast = data["list"][i]
        dt_txt = forecast["dt_txt"].split(" ")[1][:5]
        result[f"hour_{i}"] = dt_txt
        result[f"hour_{i}_temperature"] = forecast["main"]["temp"]
        result[f"hour_{i}_humidity"] = forecast["main"]["humidity"]
        result[f"hour_{i}_weather_desc"] = forecast["weather"][0]["description"]

    return result

def get_ai_advice(weather: dict):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    prompt = f"""
Dữ liệu thời tiết hiện tại và dự báo:
{json.dumps(weather, ensure_ascii=False)}

Bạn là chuyên gia nông nghiệp. Hãy đưa ra lời khuyên ngắn gọn (tối đa 4 câu) về việc chăm sóc cây rau muống trong điều kiện này.
Chia thành 3 nhóm: 
- Dinh dưỡng (advice_nutrition)
- Chăm sóc (advice_care)
- Lưu ý khác (advice_note)
"""

    body = {
        "model": "openai/gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Bạn là chuyên gia nông nghiệp Việt Nam."},
            {"role": "user", "content": prompt}
        ]
    }

    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content
    except Exception as e:
        logging.error(f"❌ Error calling AI: {e}")
        return "Không lấy được dự báo AI"

# ================== MAIN LOOP ==================
if __name__ == "__main__":
    logging.info("🚀 Starting weather → AI → ThingsBoard loop")

    # Ping startup
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})

    while True:
        try:
            weather = get_openweather()
            advice = get_ai_advice(weather)

            payload = {
                "crop": "Rau muống",
                "timestamp": datetime.utcnow().isoformat(),
                "prediction": f"Nhiệt độ {weather['temperature']}°C, độ ẩm {weather['humidity']}%",
                "advice": advice,
                **weather
            }

            push_telemetry(payload)

        except Exception as e:
            logging.error(f"❌ Main loop error: {e}")

        time.sleep(INTERVAL)
