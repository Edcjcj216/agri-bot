import os
import logging
from fastapi import FastAPI, Request
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import google.generativeai as genai

# ==============================
# Config
# ==============================
THINGSBOARD_URL = os.getenv("THINGSBOARD_URL", "https://demo.thingsboard.io")
DEVICE_ID = os.getenv("DEVICE_ID")  # ID của thiết bị trong ThingsBoard
TB_TOKEN = os.getenv("TB_TOKEN")    # Access token của thiết bị
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot")

app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

genai.configure(api_key=GEMINI_API_KEY)

# ==============================
# Pydantic Models
# ==============================
class SharedPayload(BaseModel):
    hoi: str = None
    crop: str = None
    location: str = None

class WebhookBody(BaseModel):
    shared: SharedPayload

# ==============================
# ThingsBoard helpers
# ==============================
def save_shared_attributes(attrs: dict):
    url = f"{THINGSBOARD_URL}/api/v1/{TB_TOKEN}/attributes"
    try:
        res = requests.post(url, json={"shared": attrs}, timeout=5)
        res.raise_for_status()
        logger.info(f"✅ Saved shared attributes: {attrs}")
    except Exception as e:
        logger.error(f"❌ Failed to save shared attributes: {e}")

def save_telemetry(telemetry: dict):
    url = f"{THINGSBOARD_URL}/api/v1/{TB_TOKEN}/telemetry"
    try:
        res = requests.post(url, json=telemetry, timeout=5)
        res.raise_for_status()
        logger.info(f"✅ Saved telemetry: {telemetry}")
    except Exception as e:
        logger.error(f"❌ Failed to save telemetry: {e}")

# ==============================
# AI Advice generator
# ==============================
def generate_ai_advice(question: str, crop: str = None, location: str = None) -> str:
    prompt = f"""Bạn là chuyên gia nông nghiệp.
    Câu hỏi: {question}
    Cây trồng: {crop or 'Không rõ'}
    Địa điểm: {location or 'Không rõ'}
    Hãy đưa ra lời khuyên chi tiết, rõ ràng, dễ hiểu.
    """
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip() if response and response.text else "Không tạo được lời khuyên."
    except Exception as e:
        logger.error(f"❌ AI generation error: {e}")
        return "Lỗi khi sinh lời khuyên."

# ==============================
# Routes
# ==============================

@app.get("/")
def root():
    """Render health check or simple test"""
    return {"status": "running", "service": "Agri-Bot AI"}

@app.post("/tb-webhook")
async def tb_webhook(body: WebhookBody):
    shared = body.shared.dict()
    # 1. Lưu Shared attributes vào ThingsBoard
    save_shared_attributes(shared)

    # 2. Sinh AI advice từ câu hỏi
    advice_text = generate_ai_advice(shared.get("hoi"), shared.get("crop"), shared.get("location"))

    # 3. Lưu Telemetry
    save_telemetry({"advice_text": advice_text})

    return {"status": "ok", "advice": advice_text}

# ==============================
# Scheduled Push (optional)
# ==============================
def scheduled_push():
    # Ví dụ đẩy trạng thái lên ThingsBoard mỗi phút
    save_telemetry({"service_status": "alive"})

scheduler.add_job(scheduled_push, "interval", minutes=1)

@app.on_event("startup")
def startup_event():
    logger.info("🚀 Agri-Bot AI service started.")

