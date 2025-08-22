import os
import json
import logging
import asyncio
import random
from datetime import datetime
from fastapi import FastAPI, Request
import httpx

logging.basicConfig(level=logging.INFO)
app = FastAPI()

# ================== CONFIG ==================
SEND_INTERVAL = 300  # 5 phút
LOCAL_WEBHOOK = "http://127.0.0.1:10000/tb-webhook"

# ================== FastAPI Endpoints ==================
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    try:
        body = await req.json()
        logging.info("📩 Got payload:")
        logging.info(json.dumps(body, ensure_ascii=False, indent=2))

        shared = body.get("shared", {})
        advice_text = f"AI advice placeholder for crop {shared.get('crop','unknown')}"

        return {"status": "ok", "advice_text": advice_text}
    except Exception as e:
        logging.error(f"❌ Error handling webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

@app.get("/")
def root():
    return {"status": "running"}

# ================== Payload Generator ==================
def generate_payload():
    crops = ["rau muống","cà chua","lúa"]
    questions = ["cách trồng rau muống","tưới nước cho cà chua","bón phân cho lúa"]
    payload = {
        "shared": {
            "hoi": random.choice(questions),
            "crop": random.choice(crops),
            "location": "Hồ Chí Minh",
            "temperature": round(24 + 8 * random.random(), 1),
            "humidity": round(60 + 30 * random.random(), 1),
            "battery": round(3.5 + 0.7 * random.random(), 2),
        }
    }
    return payload

# ================== Auto-send Task ==================
async def auto_send_payload():
    async with httpx.AsyncClient() as client:
        while True:
            payload = generate_payload()
            try:
                response = await client.post(LOCAL_WEBHOOK, json=payload, timeout=10)
                data = response.json()
                logging.info(f"✅ Payload sent at {datetime.now().strftime('%H:%M:%S')}")
                logging.info(f"AI advice: {data.get('advice_text')}")
            except Exception as e:
                logging.warning(f"❌ Failed to send payload: {e}")
            await asyncio.sleep(SEND_INTERVAL)

# ================== Startup Event ==================
@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Starting auto-send payload task...")
    asyncio.create_task(auto_send_payload())

# ================== Run Server ==================
if __name__ == "__main__":
    import uvicorn
    logging.info("🚀 Starting FastAPI server on http://127.0.0.1:10000")
    uvicorn.run(app, host="127.0.0.1", port=10000)
