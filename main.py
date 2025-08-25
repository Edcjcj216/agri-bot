import os
import httpx
from fastapi import FastAPI, Request
from datetime import datetime

app = FastAPI()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TB_TOKEN = os.getenv("TB_TOKEN")  # Device token ThingsBoard
TB_URL = "https://thingsboard.cloud/api/v1"


def call_gemini(prompt: str) -> str:
    """Gọi Gemini API và trả về text."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-1.5-flash-latest:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload)
        if resp.status_code != 200:
            return f"[Gemini error {resp.status_code}] {resp.text}"
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"[Gemini exception] {e}"


def push_telemetry(payload: dict):
    """Gửi kết quả trả lời về ThingsBoard telemetry."""
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        print(f"[TB] Telemetry pushed: {resp.status_code}, {payload}")
    except Exception as e:
        print(f"[TB] Error pushing telemetry: {e}")


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Simple Q&A bot running"}


@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    """
    Nhận ATTRIBUTES_UPDATED từ ThingsBoard,
    tìm trường 'ai_question', gọi Gemini trả lời,
    và đẩy câu trả lời về telemetry.
    """
    body = await req.json()
    print("[Webhook] incoming:", body)

    # Lấy câu hỏi từ shared attributes
    ai_question = None
    if "shared" in body and isinstance(body["shared"], dict):
        ai_question = body["shared"].get("ai_question")
    if not ai_question:
        return {"status": "ignored", "reason": "no ai_question"}

    # Gọi Gemini
    ai_answer = call_gemini(ai_question)

    # Push telemetry về ThingsBoard
    push_telemetry({
        "ai_question": ai_question,
        "ai_answer": ai_answer,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "question": ai_question, "answer": ai_answer}
