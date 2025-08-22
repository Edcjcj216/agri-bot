# main.py
import os
import json
import logging
from datetime import datetime
from fastapi import FastAPI, Request
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot")

app = FastAPI()

# Config
TB_TOKEN = os.getenv("TB_TOKEN")   # set this in Render env when ready
PORT = int(os.getenv("PORT", 10000))

# Status inspectors
_last_push = {"ok": False, "status": None, "body": None, "time": None}
_last_payload = {"payload": None, "time": None}

def make_advice_text(shared: dict) -> str:
    crop = shared.get("crop", "unknown")
    hoi = shared.get("hoi", "")
    return f"AI advice placeholder for crop {crop} — question: {hoi}"

async def push_to_thingsboard(payload: dict):
    """
    Push only advice_text to ThingsBoard. Update _last_push with result.
    """
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
            status = resp.status_code
            body_text = resp.text
            if 200 <= status < 300:
                logger.info(f"✅ Sent to ThingsBoard ({status}): {payload}")
                _last_push.update({"ok": True, "status": status, "body": body_text, "time": datetime.utcnow().isoformat()})
            else:
                logger.warning(f"❌ TB push failed ({status}): {body_text}")
                _last_push.update({"ok": False, "status": status, "body": body_text, "time": datetime.utcnow().isoformat()})
        except Exception as e:
            logger.exception(f"❌ Exception pushing to ThingsBoard: {e}")
            _last_push.update({"ok": False, "status": "exception", "body": str(e), "time": datetime.utcnow().isoformat()})

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    """
    Accept external payloads (from PowerShell or other clients),
    compute advice_text and push (only advice_text) to ThingsBoard.
    Also record last payload.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}

    logger.info("📩 Received payload:")
    logger.info(json.dumps(body, ensure_ascii=False, indent=2))

    shared = body.get("shared", {})
    advice_text = make_advice_text(shared)

    # record last payload
    _last_payload.update({"payload": body, "time": datetime.utcnow().isoformat()})

    # push only advice_text
    await push_to_thingsboard({"advice_text": advice_text})

    return {"status": "ok", "advice_text": advice_text}

@app.get("/")
def root():
    return {"status": "running"}

@app.get("/last-push")
def last_push():
    """Return last push status for debugging (no secrets)."""
    return _last_push

@app.get("/last-payload")
def last_payload():
    """Return last received payload (for quick debugging)."""
    return _last_payload

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
