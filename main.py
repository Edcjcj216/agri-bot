import os
import json
import requests
import httpx
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_BASE_URL = "https://thingsboard.cloud"
TB_DEVICE_TOKEN = os.getenv("TB_DEMO_TOKEN", "")
OWM_API_KEY = os.getenv("OWM_API_KEY", "")

# AI API keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
HUGGINGFACE_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN", "")

# FastAPI app
app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

# =============== WEATHER ====================
def get_weather(location: str):
    """Fetch weather forecast from OpenWeatherMap"""
    try:
        url = "http://api.openweathermap.org/data/2.5/forecast"
        params = {"q": location, "appid": OWM_API_KEY, "units": "metric", "lang": "vi"}
        res = requests.get(url, params=params, timeout=10)
        data = res.json()

        if "list" not in data:
            return {}

        forecast = {}
        for i in range(7):  # 7 hours ahead
            hour = data["list"][i]
            forecast[f"hour_{i}_temperature"] = hour["main"]["temp"]
            forecast[f"hour_{i}_humidity"] = hour["main"]["humidity"]
            forecast[f"hour_{i}_weather_desc"] = hour["weather"][0]["description"]

        # Today summary
        forecast["temperature"] = data["list"][0]["main"]["temp"]
        forecast["humidity"] = data["list"][0]["main"]["humidity"]
        forecast["weather_today_desc"] = data["list"][0]["weather"][0]["description"]

        return forecast
    except Exception as e:
        print("Weather error:", e)
        return {}

# =============== AI CLIENT ==================
async def ask_ai(question: str, crop: str, location: str, weather: dict) -> str:
    """Query AI API to generate advice"""
    prompt = f"""
Bạn là chuyên gia nông nghiệp.
Người dùng hỏi: {question}
Cây trồng: {crop}
Địa điểm: {location}
Thời tiết: {json.dumps(weather, ensure_ascii=False)}

→ Hãy đưa ra 1 đoạn lời khuyên duy nhất, súc tích, dễ hiểu.
"""

    # 1. Try OpenAI
    if OPENAI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print("OpenAI error:", e)

    # 2. Try OpenRouter
    if OPENROUTER_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "HTTP-Referer": "https://github.com/Edcjcj216/agri-bot",
                        "X-Title": "Agri-Bot",
                    },
                    json={
                        "model": "openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print("OpenRouter error:", e)

    # 3. Try Gemini
    if GEMINI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print("Gemini error:", e)

    # 4. Try Hugging Face
    if HUGGINGFACE_API_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api-inference.huggingface.co/models/facebook/blenderbot-400M-distill",
                    headers={"Authorization": f"Bearer {HUGGINGFACE_API_TOKEN}"},
                    json={"inputs": prompt},
                )
                return r.json()[0]["generated_text"].strip()
        except Exception as e:
            print("HuggingFace error:", e)

    return "Xin lỗi, hiện tại hệ thống AI không khả dụng. Vui lòng thử lại sau."

# =============== THINGSBOARD =================
def push_telemetry(payload: dict):
    url = f"{TB_BASE_URL}/api/v1/{TB_DEVICE_TOKEN}/telemetry"
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("TB Push:", res.status_code, payload)
    except Exception as e:
        print("TB error:", e)

# =============== HANDLERS ====================
@app.post("/webhook")
async def tb_webhook(req: Request):
    """Receive shared attributes from ThingsBoard"""
    data = await req.json()
    print("Webhook data:", data)

    shared = data.get("shared", {})
    question = shared.get("hoi", "Tư vấn nông nghiệp")
    crop = shared.get("crop", "rau muống")
    location = shared.get("location", "Hồ Chí Minh")

    # Lấy thời tiết
    weather = get_weather(location)

    # Gọi AI
    advice = await ask_ai(question, crop, location, weather)

    # Push duy nhất advice_text
    push_telemetry({"advice_text": advice})

    return {"status": "ok", "advice_text": advice}

# Auto push ping để tránh timeout
def auto_ping():
    push_telemetry({"advice_text": "Ping: server đang hoạt động."})

scheduler.add_job(auto_ping, "interval", minutes=5)

# Run local
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
