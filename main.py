import os
import httpx
from fastapi import FastAPI, Request

app = FastAPI()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def call_gemini(prompt: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-1.5-flash-latest:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
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

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Gemini bot is running"}

@app.post("/ask")
async def ask(req: Request):
    body = await req.json()
    question = body.get("question", "")
    if not question:
        return {"error": "Missing 'question' field"}
    answer = call_gemini(question)
    return {"question": question, "answer": answer}
