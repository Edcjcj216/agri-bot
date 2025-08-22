# main.py
import os
import json
import logging
import asyncio
import random
from datetime import datetime
from fastapi import FastAPI, Request
import httpx

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot")

# ---------- App ----------
app = FastAPI()

# ---------- Config (env-friendly) ----------
SEND_INTERVAL = int(os.getenv("SEND_INTERVAL_SECONDS", 300))  # default 300s (5 phút)
TB_TOKEN = os.getenv("TB_TOKEN")  # Render Secret (optional)
PORT = int(os.getenv("PORT", 10000))
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL")  # optional, not required

if TB_TOKEN:
    logger.info("TB_TOKEN present — will push advice_text to ThingsBoard.")
else:
    # no warning-level noise if not configured
    logger.info("TB_TOKEN not set — running in local/demo mode (no push to ThingsBoard).")

# ---------- Helper: generate advice_text ----------
def make_advice_text(shared: dict) -> str:
    # simple deterministic advice text generator — replace with real AI logic if needed
    crop = shared.get("crop", "unknown")
    hoi = shared.get("hoi", "")
    return f"AI advice placeholder for crop {crop} — question: {hoi}"

# ---------- ThingsBoard push ----------
async def push_to_thingsboard(payload: dict):
    """
    Push only advice_text (single key) to ThingsBoard telemetry.
    If TB_TOKEN not set, function returns silently after logging info.
    """
    if not TB_TOKEN:
        return

    url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"
    # ThingsBoard prefers timestamp in ms under _ts
    payload["_ts"] = int(datetime.utcnow().timestamp() * 1000)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(f"✅ Sent to ThingsBoard: {payload}")
        except Exception as e:
            # keep this as warning/error so operator can see actual push failures
            logger.warning(f"❌ Failed to push to ThingsBoard: {e}")

# ---------- FastAPI endpoints ----------
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    """
    Accept external payloads (e.g., from a device or external script).
    We compute advice_text and (if configured) push advice_text to ThingsBoard.
    """
    try:
        body = await req.json()
    except Exception:
        # fallback if body not JSON
        body = {}

    logger.info("📩 Received external payload:")
    logger.info(json.dumps(body, ensure_ascii=False, indent=2))

    shared = body.get("shared", {})
    advice_text = make_advice_text(shared)

    # push only advice_text
    await push_to_thingsboard({"advice_text": advice_text})

    return {"status": "ok", "advice_text": advice_text}

@app.get("/")
def root():
    return {"status": "running"}

# ---------- Auto-send task (DIRECT CALL, no internal HTTP) ----------
def generate_payload():
    crops = ["rau muống", "cà chua", "lúa"]
    questions = ["cách trồng rau muống", "tưới nước cho cà chua", "bón phân cho lúa"]
    return {
        "shared": {
            "hoi": random.choice(questions),
            "crop": random.choice(crops),
            "location": "Hồ Chí Minh",
        }
    }

async def auto_send_loop():
    """
    Instead of doing an HTTP POST to our own /tb-webhook (which caused connection failures),
    we call the handler logic directly: generate payload -> compute advice_text -> push to TB.
    """
    logger.info("🚀 Auto-send loop started (direct calls). Interval: %s seconds", SEND_INTERVAL)
    while True:
        payload = generate_payload()
        shared = payload.get("shared", {})
        advice_text = make_advice_text(shared)

        # log locally
        logger.info("🚀 Auto-generated payload at %s", datetime.utcnow().isoformat())
        logger.info(json.dumps(payload, ensure_ascii=False))

        # push only advice_text
        await push_to_thingsboard({"advice_text": advice_text})

        # Also log the advice locally
        logger.info("AI advice: %s", advice_text)

        await asyncio.sleep(SEND_INTERVAL)

# ---------- Startup ----------
@app.on_event("startup")
async def on_startup():
    # start background auto-send task
    asyncio.create_task(auto_send_loop())

# ---------- Run ----------
if __name__ == "__main__":
    import uvicorn
    logger.info(f"🚀 Starting server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
