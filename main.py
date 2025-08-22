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
SEND_INTERVAL = int(os.getenv("SEND_INTERVAL_SECONDS", 300))  # default 300s (5 minutes)
TB_TOKEN = os.getenv("TB_TOKEN")  # Render Secret (optional)
PORT = int(os.getenv("PORT", 10000))

if TB_TOKEN:
    logger.info("TB_TOKEN present — will push advice_text to ThingsBoard.")
else:
    # do not spam warning; use info so logs are cleaner
    logger.info("TB_TOKEN not set — running in local/demo mode (no push to ThingsBoard).")

# ---------- Last push status (for /last-push) ----------
_last_push = {"ok": False, "status": None, "body": None, "time": None}

# ---------- Helper: generate advice_text ----------
def make_advice_text(shared: dict) -> str:
    crop = shared.get("crop", "unknown")
    hoi = shared.get("hoi", "")
    # Simple deterministic advice placeholder — replace with real AI logic if needed
    return f"AI advice placeholder for crop {crop} — question: {hoi}"

# ---------- ThingsBoard push (detailed logging) ----------
async def push_to_thingsboard(payload: dict):
    global _last_push
    if not TB_TOKEN:
        _last_push.update({"ok": False, "status": "no_token", "body": None, "time": datetime.utcnow().isoformat()})
        logger.info("TB_TOKEN not set — skipping push to ThingsBoard.")
        return

    url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"
    payload["_ts"] = int(datetime.utcnow().timestamp() * 1000)

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=10)
            text = resp.text
            status = resp.status_code
            if 200 <= status < 300:
                logger.info(f"✅ Sent to ThingsBoard ({status}): {payload}")
                _last_push.update({"ok": True, "status": status, "body": text, "time": datetime.utcnow().isoformat()})
            else:
                logger.warning(f"❌ TB push failed ({status}): {text}")
                _last_push.update({"ok": False, "status": status, "body": text, "time": datetime.utcnow().isoformat()})
        except Exception as e:
            logger.exception(f"❌ Exception pushing to ThingsBoard: {e}")
            _last_push.update({"ok": False, "status": "exception", "body": str(e), "time": datetime.utcnow().isoformat()})

# ---------- FastAPI endpoints ----------
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
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

@app.get("/last-push")
def last_push():
    """Return last push status for debugging."""
    return _last_push

# ---------- Auto-send task (direct call, no internal HTTP) ----------
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
    logger.info("🚀 Auto-send loop started (direct calls). Interval: %s seconds", SEND_INTERVAL)
    while True:
        payload = generate_payload()
        shared = payload.get("shared", {})
        advice_text = make_advice_text(shared)

        logger.info("🚀 Auto-generated payload at %s", datetime.utcnow().isoformat())
        logger.info(json.dumps(payload, ensure_ascii=False))

        # push only advice_text
        await push_to_thingsboard({"advice_text": advice_text})

        # local log
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
