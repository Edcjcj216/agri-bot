import os
import json
import logging
import requests
import httpx
from datetime import datetime
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
from pathlib import Path

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "kRCUZFGs9gq5GAkIWZTq")  # device mới

logging.basicConfig(level=logging.INFO)
app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

LOG_FILE = Path("/tmp/tb_payloads.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def push_to_tb(data: dict):
    if not TB_TOKEN:
        logging.error("❌ ThingsBoard token chưa được cấu hình!")
        return
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    data["_ts"] = int(datetime.utcnow().timestamp() * 1000)
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard: {data}")
    except Exception as e:
        logging.error(f"❌ Failed to push telemetry: {e}")

def log_payload(payload: dict):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.utcnow().isoformat()}] {json.dumps(payload, ensure_ascii=False)}\n")
    except Exception as e:
        logging.error(f"❌ Failed to log payload: {e}")

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    try:
        body = await req.json()
        logging.info("📩 Got TB webhook payload:")
        logging.info(json.dumps(body, ensure_ascii=False, indent=2))
        log_payload(body)

        shared = body.get("shared", {})
        advice_text = f"AI advice placeholder for crop {shared.get('crop','unknown')}"
        push_to_tb({"advice_text": advice_text})

        return {"status": "ok", "advice_text": advice_text}
    except Exception as e:
        logging.error(f"❌ Error handling webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

@app.get("/")
def root():
    return {"status": "running"}

@app.on_event("startup")
def init():
    logging.info("🚀 Agri-Bot Python server started")
