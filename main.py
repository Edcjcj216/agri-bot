import os
import logging
import httpx
from fastapi import FastAPI, Request
from datetime import datetime

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")   # Device token của bạn
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== APP ==================
app = FastAPI()

# ================== THINGSBOARD PUSH ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"[TB] Push telemetry {payload} | Status {resp.status_code}")
    except Exception as e:
        logger.error(f"[TB] Telemetry error: {e}")

def push_attributes(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/attributes"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"[TB] Push attributes {payload} | Status {resp.status_code}")
    except Exception as e:
        logger.error(f"[TB] Attributes error: {e}")

# ================== AI CALL ==================
def generate_ai_reply(question: str) -> str:
    if not GEMINI_API_KEY:
        return f"(Mock trả lời) Bạn hỏi: {question}"
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(question)
        return resp.text.strip()
    except Exception as e:
        logger.error(f"[AI] Error: {e}")
        return "(Lỗi AI)"

# ================== ROUTES ==================
@app.get("/")
def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.head("/")
def health_check_head():
    return {"status": "ok"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    data = await req.json()
    shared = data.get("shared", {})

    # 1. Lấy câu hỏi từ Shared attributes
    hoi = shared.get("hoi", "")
    logger.info(f"[Webhook] Nhận hoi = {hoi}")

    # 2. Lưu shared attributes lại (optional, để chắc chắn)
    push_attributes({"hoi": hoi})

    # 3. Gọi AI sinh trả lời
    tra_loi = generate_ai_reply(hoi)

    # 4. Lưu trả lời lên telemetry
    push_telemetry({
        "hoi": hoi,
        "tra_loi": tra_loi,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "hoi": hoi, "tra_loi": tra_loi}

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logger.info("[INFO] App started.")
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
