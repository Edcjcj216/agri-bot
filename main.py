import os
import logging
from datetime import datetime
from fastapi import FastAPI, Request
import httpx

# ====== CONFIG ======
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")        # Device Access Token trên ThingsBoard
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # API key của Gemini

# ====== LOGGING ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hoi-bot")

# ====== FASTAPI APP ======
app = FastAPI()

# ====== PUSH LÊN THINGSBOARD ======
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"Telemetry pushed: {payload}, status={resp.status_code}")
    except Exception as e:
        logger.error(f"Error push telemetry: {e}")

# ====== GỌI GEMINI ======
def generate_ai_reply(question: str) -> str:
    if not GEMINI_API_KEY:
        return f"(mock) Bạn hỏi: {question}"
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(question)
        return resp.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "(error khi gọi AI)"

# ====== ROUTES ======
@app.get("/")
def health():
    return {"status": "ok", "message": "server chạy tốt"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    data = await req.json()
    shared = data.get("shared", {})
    hoi = shared.get("hoi", "")

    logger.info(f"Nhận shared.hoi = {hoi}")

    if not hoi:
        return {"status": "no question"}

    # Gọi AI trả lời
    tra_loi = generate_ai_reply(hoi)

    # Lưu telemetry lên ThingsBoard
    push_telemetry({
        "hoi": hoi,
        "tra_loi": tra_loi,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "hoi": hoi, "tra_loi": tra_loi}
