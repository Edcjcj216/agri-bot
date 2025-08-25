import os
import logging
from fastapi import FastAPI, Request
import httpx
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tb-webhook")

THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # Access token của device
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

app = FastAPI()

@app.get("/")
async def health_check():
    return {"status": "ok"}

@app.post("/tb-webhook")
async def tb_webhook(request: Request):
    data = await request.json()
    logger.info(f"Webhook data: {data}")

    question = data.get("hoi")
    if not question:
        # không có key hoi
        await push_telemetry({"status": "no question"})
        return {"status": "no question"}

    # ở đây thay bằng AI call thực tế nếu muốn
    advice_text = f"Trả lời: {question}"

    await push_telemetry({"advice_text": advice_text})
    return {"status": "ok", "advice_text": advice_text}

async def push_telemetry(payload: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(THINGSBOARD_URL, json=payload)
        logger.info(f"Pushed telemetry: {payload} | Status {r.status_code}")

@app.on_event("startup")
async def startup_event():
    now = datetime.utcnow().isoformat()
    await push_telemetry({"startup_ping": now})
    logger.info("Starting server...")
