import os
import logging
from fastapi import FastAPI, Request
import httpx
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tb-webhook")

THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

app = FastAPI()

@app.get("/")
async def health_check():
    return {"status": "ok"}

@app.post("/tb-webhook")
async def tb_webhook(request: Request):
    data = await request.json()
    logger.info(f"Webhook data: {data}")

    # Tìm "hoi" ở bất kỳ đâu trong payload
    question = find_key(data, "hoi")

    if not question:
        await push_telemetry({"status": "no question"})
        return {"status": "no question"}

    advice_text = f"Trả lời: {question}"
    await push_telemetry({"advice_text": advice_text})
    return {"status": "ok", "advice_text": advice_text}

def find_key(data, key):
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                return v
            result = find_key(v, key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_key(item, key)
            if result is not None:
                return result
    return None

async def push_telemetry(payload: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(THINGSBOARD_URL, json=payload)
        logger.info(f"Pushed telemetry: {payload} | Status {r.status_code}")

@app.on_event("startup")
async def startup_event():
    now = datetime.utcnow().isoformat()
    await push_telemetry({"startup_ping": now})
    logger.info("Starting server...")
