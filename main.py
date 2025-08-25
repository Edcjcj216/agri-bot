import os
import logging
from fastapi import FastAPI, Request
import httpx
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tb-webhook")

app = FastAPI()

TB_URL = os.getenv("TB_URL", "https://thingsboard.cloud")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")   # lấy token TB device
GEMINI_KEY = os.getenv("GEMINI_KEY")       # key Gemini API

@app.on_event("startup")
async def startup_event():
    # Gửi ping lên ThingsBoard để biết server đang sống
    async with httpx.AsyncClient() as client:
        payload = {"startup_ping": datetime.utcnow().isoformat()}
        url = f"{TB_URL}/api/v1/{DEVICE_TOKEN}/telemetry"
        try:
            r = await client.post(url, json=payload)
            logger.info(f"Pushed telemetry: {payload} | Status {r.status_code}")
        except Exception as e:
            logger.error(f"Failed to push startup ping: {e}")

@app.get("/")
async def root():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.post("/tb-webhook")
async def tb_webhook(request: Request):
    data = await request.json()
    logger.info(f"Received webhook: {data}")

    # Lấy câu hỏi từ payload
    question = data.get("hoi")
    if not question:
        # Gửi telemetry báo không có câu hỏi
        await push_telemetry({"status": "no question"})
        return {"status": "no question"}

    # Gọi Gemini API
    advice_text = await ask_gemini(question)
    logger.info(f"Gemini answer: {advice_text}")

    # Đẩy câu trả lời lên ThingsBoard telemetry
    await push_telemetry({"advice_text": advice_text})
    return {"status": "ok", "answer": advice_text}


async def ask_gemini(question: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_KEY}"
    body = {
        "contents": [
            {"parts": [{"text": question}]}
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return "AI service error"


async def push_telemetry(payload: dict):
    url = f"{TB_URL}/api/v1/{DEVICE_TOKEN}/telemetry"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload)
            logger.info(f"Pushed telemetry: {payload} | Status {r.status_code}")
        except Exception as e:
            logger.error(f"Failed to push telemetry: {e}")
