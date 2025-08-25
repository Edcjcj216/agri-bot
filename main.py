import os
import logging
import httpx
from datetime import datetime
from fastapi import FastAPI, Request

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")                 # Device token TB
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")     # Gemini API key

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot")

# ================== APP ==================
app = FastAPI()

# ================== THINGSBOARD HELPERS ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(url, json=payload)
        logger.info(f"[TB Telemetry] {payload} | status {r.status_code}")
    except Exception as e:
        logger.error(f"Error pushing telemetry: {e}")

def push_attributes(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/attributes"
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(url, json=payload)
        logger.info(f"[TB Attributes] {payload} | status {r.status_code}")
    except Exception as e:
        logger.error(f"Error pushing attributes: {e}")

# ================== GEMINI AI ==================
def generate_ai_advice(prompt: str) -> str:
    import google.generativeai as genai
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "(Error generating advice)"

# ================== ROUTES ==================
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Agri-bot running"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    shared = body.get("shared", {})

    hoi = shared.get("hoi", "")
    crop = shared.get("crop", "")
    location = shared.get("location", "")

    # 1. Lưu Shared attributes lên TB
    push_attributes({"hoi": hoi, "crop": crop, "location": location})

    # 2. Gọi Gemini sinh AI advice
    prompt = f"Hãy tư vấn nông nghiệp cho câu hỏi: '{hoi}' về cây '{crop}' tại '{location}'."
    advice_text = generate_ai_advice(prompt)

    # 3. Lưu advice lên telemetry
    push_telemetry({
        "advice_text": advice_text,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "advice": advice_text}

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logger.info("Starting Agri-bot...")
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
