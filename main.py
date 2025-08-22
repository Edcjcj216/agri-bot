import os
import logging
import httpx
from fastapi import FastAPI, Request

# ------------------------
# Config
# ------------------------
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN", "sgkxcrqntuki8gu1oj8u")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyCdF-cPhDw9Mn83F-or_26TTBq0UYGcYUI")
OWM_API_KEY = os.getenv("OWM_API_KEY", "a53f443795604c41b72305c1806784db")

THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
OWM_URL = f"http://api.openweathermap.org/data/2.5/weather?q=Ho%20Chi%20Minh,vn&appid={OWM_API_KEY}&units=metric&lang=vi"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="AgriBot AI Service")


# ------------------------
# Gọi Gemini AI
# ------------------------
async def generate_advice(prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            payload = {
                "contents": [{
                    "parts": [{"text": f"Bạn là chuyên gia nông nghiệp. Hãy trả lời ngắn gọn, dễ hiểu.\n\nCâu hỏi: {prompt}"}]
                }]
            }
            resp = await client.post(GEMINI_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        return "Xin lỗi, hệ thống AI hiện không khả dụng. Vui lòng thử lại sau."


# ------------------------
# Lấy thời tiết từ OpenWeatherMap
# ------------------------
async def get_weather():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(OWM_URL)
            resp.raise_for_status()
            data = resp.json()
            return {
                "temperature": data["main"]["temp"],
                "humidity": data["main"]["humidity"],
                "weather_desc": data["weather"][0]["description"]
            }
    except Exception as e:
        logging.error(f"Weather API error: {e}")
        return {}


# ------------------------
# Gửi dữ liệu lên ThingsBoard
# ------------------------
async def push_to_thingsboard(payload: dict):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(THINGSBOARD_URL, json=payload)
            resp.raise_for_status()
            logging.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logging.error(f"❌ Failed to send to ThingsBoard: {e}")


# ------------------------
# Endpoint nhận Shared Attributes từ ThingsBoard
# ------------------------
@app.post("/webhook")
async def webhook_handler(request: Request):
    data = await request.json()
    logging.info(f"📩 Received from TB: {data}")

    try:
        shared_attrs = data.get("shared", {})
        if not shared_attrs:
            return {"status": "no shared attributes"}

        # Lấy câu hỏi từ key "hoi"
        question = shared_attrs.get("hoi", "Làm nông thế nào?")
        logging.info(f"👉 Question: {question}")

        # Gọi AI
        advice = await generate_advice(question)

        # Lấy thời tiết
        weather = await get_weather()

        # Push lên ThingsBoard
        payload = {
            "advice_text": advice,
            "question": question
        }
        if weather:
            payload.update(weather)

        await push_to_thingsboard(payload)

        return {"status": "ok", "advice": advice, "weather": weather}

    except Exception as e:
        logging.error(f"Error in webhook handler: {e}")
        return {"status": "error", "msg": str(e)}


# ------------------------
# Root test
# ------------------------
@app.get("/")
async def root():
    return {"msg": "AgriBot AI Service running 🚀"}
