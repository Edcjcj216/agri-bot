import os
import logging
import httpx
from datetime import datetime
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
OWM_API_KEY = os.getenv("OWM_API_KEY")  # OpenWeather free key
LAT = 10.8781
LON = 106.7594

TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== APP ==================
app = FastAPI()
scheduler = BackgroundScheduler()

# ================== THINGSBOARD PUSH ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"Pushing telemetry: {payload}")
        logger.info(f"Response status: {resp.status_code}, body: {resp.text}")
    except Exception as e:
        logger.error(f"Error pushing telemetry: {e}")

def push_attributes(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/attributes"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        logger.info(f"Pushing attributes: {payload}")
        logger.info(f"Response status: {resp.status_code}, body: {resp.text}")
    except Exception as e:
        logger.error(f"Error pushing attributes: {e}")

# ================== OPENWEATHER ==================
def fetch_weather():
    base_url = "https://api.openweathermap.org/data/2.5"
    current_url = f"{base_url}/weather?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric"
    forecast_url = f"{base_url}/forecast?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric"

    try:
        with httpx.Client(timeout=10) as client:
            current = client.get(current_url).json()
            forecast = client.get(forecast_url).json()
        weather_data = {
            "temperature": current["main"]["temp"],
            "humidity": current["main"]["humidity"],
            "weather_today_desc": current["weather"][0]["description"],
            "forecast_list": forecast.get("list", [])
        }
        return weather_data
    except Exception as e:
        logger.error(f"[ERROR] Error fetching OpenWeather: {e}")
        return None

# ================== AI CALL ==================
def generate_ai_advice(prompt: str):
    """
    Gọi Gemini nếu có key, nếu không có thì fallback sang prompt đơn giản.
    """
    try:
        if GEMINI_API_KEY:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-1.5-flash")
            resp = model.generate_content(prompt)
            return resp.text.strip()
        elif OPENROUTER_API_KEY:
            # Bạn có thể gọi OpenRouter API nếu muốn
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7
                    }
                )
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            # Fallback basic
            return f"(Mock advice) {prompt}"
    except Exception as e:
        logger.error(f"AI generation error: {e}")
        return "(Error generating advice)"

def generate_weather_advice(weather_data: dict):
    if not weather_data:
        return {"skipped": "no weather data"}
    advice_text = f"Nhiệt độ hiện tại {weather_data['temperature']}°C, độ ẩm {weather_data['humidity']}%, thời tiết: {weather_data['weather_today_desc']}. Điều chỉnh tưới và bón phù hợp."
    return {"advice": advice_text}

# ================== JOB ==================
def job():
    logger.info("Executing scheduled job...")
    weather_data = fetch_weather()
    if not weather_data:
        logger.error("Skipping job due to weather fetch failure")
        return

    advice = generate_weather_advice(weather_data)
    telemetry_payload = {
        "ping_time": datetime.utcnow().isoformat(),
        "temperature": weather_data["temperature"],
        "humidity": weather_data["humidity"],
        "weather_today_desc": weather_data["weather_today_desc"],
        "llm_advice": advice
    }
    push_telemetry(telemetry_payload)

# ================== ROUTES ==================
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Agri-bot is running"}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    shared = body.get("shared", {})

    hoi = shared.get("hoi", "")
    crop = shared.get("crop", "")
    location = shared.get("location", "")

    # 1. Lưu Shared attributes lên ThingsBoard
    push_attributes({
        "hoi": hoi,
        "crop": crop,
        "location": location
    })

    # 2. Sinh AI advice
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
    logger.info("[INFO] Starting app...")
    push_telemetry({"startup_ping": datetime.utcnow().isoformat()})
    # Schedule job every 5 minutes
    scheduler.add_job(job, 'interval', minutes=5, id='weather_ai_job', next_run_time=datetime.utcnow())
    scheduler.start()
