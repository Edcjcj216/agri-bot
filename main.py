import os
import logging
from datetime import datetime
from fastapi import FastAPI, Request
import httpx
import google.generativeai as genai

# ===== CONFIG =====
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")           # token thiết bị từ TB
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== AI CONFIG =====
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ===== FASTAPI APP =====
app = FastAPI()

# ===== PUSH TELEMETRY TO THINGSBOARD =====
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"Pushed telemetry: {payload} | status {resp.status_code}")
    except Exception as e:
        logger.error(f"Error pushing telemetry: {e}")

# ===== ROUTES =====
@app.get("/")
def health_check():
    return {"status": "ok", "message": "AI webhook running"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    logger.info(f"Received webhook: {body}")

    # lấy shared attributes
    shared = body.get("shared", {})
    hoi = shared.get("hoi")
    if not hoi:
        return {"status": "ignored", "reason": "no 'hoi' attribute"}

    # sinh câu trả lời bằng Gemini
    try:
        resp = model.generate_content(hoi)
        answer = resp.text.strip()
    except Exception as e:
        logger.error(f"AI generation error: {e}")
        answer = "(error generating answer)"

    # lưu lên telemetry
    push_telemetry({
        "hoi": hoi,
        "tra_loi": answer,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "hoi": hoi, "tra_loi": answer}
