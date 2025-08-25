import os
import logging
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# ==== Load ENV ====
load_dotenv()
TB_URL = os.getenv("TB_URL", "https://demo.thingsboard.io/api/v1")
TB_TOKEN = os.getenv("TB_TOKEN", "<DEVICE_TOKEN>")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "<YOUR_GEMINI_KEY>")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Agri-Bot AI Service")

# ==== ThingsBoard Helpers ====

def push_to_tb(data: dict, shared=False):
    """Đẩy dữ liệu lên ThingsBoard (Shared Attributes hoặc Telemetry)."""
    url = f"{TB_URL}/{TB_TOKEN}/attributes" if shared else f"{TB_URL}/{TB_TOKEN}/telemetry"
    payload = {"shared": data} if shared else data
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard ({'shared' if shared else 'telemetry'}): {data}")
    except Exception as e:
        logging.error(f"❌ Failed to push to ThingsBoard: {e}")

# ==== Gemini API ====

def generate_advice(question: str, crop: str, location: str) -> str:
    """
    Gọi Gemini API để sinh tư vấn nông nghiệp.
    """
    prompt = f"""
    Bạn là một chuyên gia nông nghiệp.
    Câu hỏi: {question}
    Cây trồng: {crop}
    Địa điểm: {location}
    Trả lời ngắn gọn, cụ thể và dễ áp dụng.
    """
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [
                {"parts": [{"text": prompt}]}
            ]
        }
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # Parse text từ Gemini
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        logging.error(f"❌ Gemini API error: {e}")
        return "Không thể sinh tư vấn lúc này, vui lòng thử lại sau."

# ==== Webhook Endpoint ====

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    try:
        body = await req.json()
        logging.info(f"📩 Received webhook: {body}")

        shared_data = body.get("shared", {})
        hoi = shared_data.get("hoi")
        crop = shared_data.get("crop")
        location = shared_data.get("location")

        if not hoi or not crop or not location:
            return JSONResponse({"status": "error", "msg": "Missing hoi/crop/location"}, status_code=400)

        # 1. Lưu Shared attributes
        push_to_tb({"hoi": hoi, "crop": crop, "location": location}, shared=True)

        # 2. Sinh AI advice
        advice_text = generate_advice(hoi, crop, location)

        # 3. Lưu Telemetry
        push_to_tb({"advice_text": advice_text}, shared=False)

        return {"status": "ok", "advice": advice_text}

    except Exception as e:
        logging.error(f"❌ Webhook processing failed: {e}")
        return JSONResponse({"status": "error", "msg": str(e)}, status_code=500)

# ==== Optional: Scheduled Job ====
scheduler = BackgroundScheduler()

def scheduled_push():
    logging.info("⏱️ Scheduled push completed.")
    # Ví dụ: bạn có thể thêm logic đẩy dữ liệu định kỳ nếu cần
    # push_to_tb({"status": "alive"}, shared=False)

scheduler.add_job(scheduled_push, "interval", minutes=10)
scheduler.start()

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Agri-Bot AI service started.")

