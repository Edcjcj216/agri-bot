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
TB_TOKEN = os.getenv("TB_TOKEN")  # Render Secret: TB_TOKEN
PORT = int(os.getenv("PORT", 10000))  # Render inject PORT
# Nếu TB_TOKEN chưa set, bỏ qua push nhưng vẫn chạy server
if not TB_TOKEN:
    logging.warning("⚠️ TB_TOKEN chưa được cấu hình! Chỉ log locally.")

# ================== URL webhook nội bộ ==================
# Sử dụng URL public nếu deploy Render, fallback nội bộ
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL")  # Optional: set nếu muốn dùng public URL
LOCAL_WEBHOOK = APP_PUBLIC_URL or f"http://127.0.0.1:{PORT}/tb-webhook"

# ================== FastAPI Endpoints ==================
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    logging.info("📩 Got payload:")
    logging.info(json.dumps(body, ensure_ascii=False, indent=2))

    shared = body.get("shared", {})
    advice_text = f"AI advice placeholder for crop {shared.get('crop','unknown')}"

    # Chỉ push advice_text lên ThingsBoard nếu TB_TOKEN có
    if TB_TOKEN:
        await push_to_tb({"advice_text": advice_text})

    return {"status": "ok", "advice_text": advice_text}

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
        }
    }
    return payload

# ================== ThingsBoard Push ==================
async def push_to_tb(data: dict):
    url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"
    data["_ts"] = int(datetime.utcnow().timestamp() * 1000)
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=data, timeout=10)
            r.raise_for_status()
            logging.info(f"✅ Sent to ThingsBoard: {data}")
        except Exception as e:
            logging.warning(f"❌ Failed to push telemetry: {e}")

# ================== Auto-send Task ==================
async def auto_send_payload():
    async with httpx.AsyncClient() as client:
        while True:
            payload = generate_payload()
            try:
                response = await client.post(LOCAL_WEBHOOK, json=payload, timeout=10)
                data = response.json()
                logging.info(f"🚀 Payload sent at {datetime.now().strftime('%H:%M:%S')}")
                logging.info(f"AI advice: {data.get('advice_text')}")
            except Exception as e:
                logging.warning(f"❌ Failed to send payload to /tb-webhook: {e}")
            await asyncio.sleep(SEND_INTERVAL)

# ================== Startup Event ==================
@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Starting auto-send payload task...")
    asyncio.create_task(auto_send_payload())

# ================== Run Server ==================
if __name__ == "__main__":
    import uvicorn
    logging.info(f"🚀 Starting FastAPI server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
