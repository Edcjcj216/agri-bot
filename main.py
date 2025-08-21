import os
import logging
import requests
import httpx
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ==============================
# Cấu hình token
# ==============================
AI_TOKEN = os.getenv("AI_TOKEN", "demo_ai_token")  # token AI
TB_TOKEN = "pk94asonfacs6mbeuutg"  # token ThingsBoard cố định
TB_URL = f"https://demo.thingsboard.io/api/v1/{TB_TOKEN}/telemetry"

logging.info(f"[DEBUG] AI_TOKEN = {AI_TOKEN}")
logging.info(f"[DEBUG] TB_TOKEN = {TB_TOKEN}")

app = FastAPI()

# ==============================
# Map mã thời tiết → mô tả tiếng Việt
# ==============================
WEATHER_CODE_MAP = {
    0: "Trời quang đãng",
    1: "Chủ yếu quang",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù đông đá",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá to",
}

# ==============================
# Hàm lấy thời tiết từ Open-Meteo
# ==============================
def get_weather(lat: float, lon: float):
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,weathercode"
        f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
        f"&forecast_days=2&timezone=auto&lang=vi"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # Dữ liệu hôm nay và ngày mai
    daily = data["daily"]
    hom_nay = {
        "weather_desc": WEATHER_CODE_MAP.get(daily["weathercode"][0], "Không rõ"),
        "temp_max": daily["temperature_2m_max"][0],
        "temp_min": daily["temperature_2m_min"][0],
    }
    ngay_mai = {
        "weather_desc": WEATHER_CODE_MAP.get(daily["weathercode"][1], "Không rõ"),
        "temp_max": daily["temperature_2m_max"][1],
        "temp_min": daily["temperature_2m_min"][1],
    }

    # Lấy 6 giờ tiếp theo từ hourly
    hourly = data["hourly"]
    now = datetime.now().hour
    next6 = {}
    for i in range(6):
        idx = now + i if now + i < len(hourly["temperature_2m"]) else -1
        temp = hourly["temperature_2m"][idx]
        code = hourly["weathercode"][idx]
        desc = WEATHER_CODE_MAP.get(code, "Không rõ")
        next6[f"hour_{i+1}"] = {"temp": temp, "weather_desc": desc}

    return hom_nay, ngay_mai, next6

# ==============================
# Hàm gọi AI API
# ==============================
async def call_ai_api(data: dict):
    if not AI_TOKEN or AI_TOKEN.strip() == "":
        logging.warning("[AI] Token rỗng hoặc không hợp lệ!")
        return {}

    url = "https://ai.example.com/predict"  # đổi theo endpoint thật
    headers = {"Authorization": f"Bearer {AI_TOKEN}"}

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(url, json=data, headers=headers)
            logging.info(f"AI ◀ status={resp.status_code}")
            if resp.status_code != 200:
                logging.warning(f"AI API returned {resp.status_code}: {resp.text}")
                return {}
            return resp.json()
        except Exception as e:
            logging.error(f"AI API error: {e}")
            return {}

# ==============================
# Hàm gửi telemetry lên ThingsBoard
# ==============================
def send_to_tb(payload: dict):
    try:
        r = requests.post(TB_URL, json=payload, timeout=10)
        if r.status_code != 200:
            logging.warning(f"[TB] HTTP {r.status_code}: {r.text}")
        else:
            logging.info(f"TB ◀ OK {r.status_code}")
    except Exception as e:
        logging.error(f"[TB] Error sending data: {e}")

# ==============================
# API nhận dữ liệu từ ESP32
# ==============================
@app.post("/esp32-data")
async def esp32_data(req: Request):
    body = await req.json()
    temp = body.get("temperature")
    hum = body.get("humidity")
    bat = body.get("battery")
    crop = body.get("crop", "Không rõ")
    lat = body.get("lat")
    lon = body.get("lon")

    # Lấy thời tiết
    hom_nay, ngay_mai, next6 = get_weather(lat, lon)

    # Gọi AI API để sinh gợi ý
    ai_input = {
        "temperature": temp,
        "humidity": hum,
        "battery": bat,
        "crop": crop,
        "hom_nay": hom_nay,
        "ngay_mai": ngay_mai,
        "next6": next6
    }
    ai_result = await call_ai_api(ai_input)

    payload = {
        "temperature": temp,
        "humidity": hum,
        "battery": bat,
        **ai_result,
        "hom_nay": hom_nay,
        "ngay_mai": ngay_mai,
        **next6
    }
    logging.info(f"TB ▶ {payload}")
    send_to_tb(payload)
    return {"status": "ok", "sent": payload}

# ==============================
# Auto scheduler demo
# ==============================
def auto_loop():
    logging.info("Auto loop running...")

scheduler = BackgroundScheduler()
scheduler.add_job(auto_loop, "interval", minutes=30)
scheduler.start()

# ==============================
# Main
# ==============================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
