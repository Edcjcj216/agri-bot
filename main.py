import os
import httpx
from fastapi import FastAPI, Request
from datetime import datetime

app = FastAPI()

# Lấy API key Gemini và ThingsBoard token từ environment
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TB_TOKEN = os.getenv("TB_TOKEN")
TB_URL = "https://thingsboard.cloud/api/v1"

def call_gemini(prompt: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-1.5-flash-latest:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, headers=headers, json=data)
        if resp.status_code != 200:
            return f"Gemini API error {resp.status_code}: {resp.text}"
        result = resp.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Error calling Gemini: {e}"

def push_telemetry(payload: dict):
    """Gửi kết quả trả lời về ThingsBoard device telemetry."""
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        print(f"[TB] Pushed telemetry: {payload}, status={resp.status_code}")
    except Exception as e:
        print(f"[TB] Error pushing telemetry: {e}")

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Gemini TB-bot is running"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    """
    Nhận ATTRIBUTES_UPDATED từ ThingsBoard,
    tìm trường 'ai_question', gọi Gemini trả lời,
    rồi push lại kết quả lên telemetry.
    """
    body = await req.json()
    print("[TB] Incoming webhook:", body)

    # Lấy câu hỏi từ shared attributes hoặc msg
    ai_question = None
    if "shared" in body and isinstance(body["shared"], dict):
        ai_question = body["shared"].get("ai_question")
    elif "ai_question" in body:
        ai_question = body.get("ai_question")

    if not ai_question:
        return {"status": "ignored", "reason": "no ai_question"}

    # Gọi Gemini sinh câu trả lời
    answer = call_gemini(ai_question)

    # Gửi trả lời về TB
    push_telemetry({
        "ai_question": ai_question,
        "ai_answer": answer,
        "time": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "question": ai_question, "answer": answer}
