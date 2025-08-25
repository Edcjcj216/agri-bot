import os
import logging
from datetime import datetime
from fastapi import FastAPI, Request
import httpx

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")  # Device access token trên ThingsBoard
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # Key Gemini AI

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tb-webhook")

# ================== APP ==================
app = FastAPI()

# ================== THINGSBOARD PUSH ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"Pushed telemetry: {payload} | Status {resp.status_code}")
    except Exception as e:
        logger.error(f"Error pushing telemetry: {e}")

# ================== AI CALL ==================
def generate_ai_reply(question: str) -> str:
    try:
        if not GEMINI_API_KEY:
            return f"(No GEMINI_API_KEY) Bạn hỏi: {question}"
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(question)
        return resp.text.strip()
    except Exception as e:
        logger.error(f"AI generation error: {e}")
        return f"(Error) Bạn hỏi: {question}"

# ================== ROUTES ==================
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Server running"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    data = await req.json()
    shared = data.get("shared", {})
    hoi = shared.get("hoi", "")

    logger.info(f"Received question: {hoi}")

    # 1. Sinh câu trả lời bằng AI
    answer = generate_ai_reply(hoi)

    # 2. Đẩy lên ThingsBoard telemetry
    push_telemetry({
        "hoi": hoi,
        "answer": answer,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "answer": answer}

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logger.info("Starting server...")
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
