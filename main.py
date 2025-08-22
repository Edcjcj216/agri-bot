import os
import json
import logging
import requests
import httpx
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
HF_KEY = os.getenv("HUGGINGFACE_API_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
OWM_KEY = os.getenv("OWM_API_KEY")  # OpenWeatherMap API key (optional)

# để nguyên phần thời tiết
logging.basicConfig(level=logging.INFO)
app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

# ============= AI CLIENTS ==============
async def ask_openai(prompt: str) -> str:
    if not OPENAI_KEY:
        raise ValueError("Missing OPENAI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def ask_openrouter(prompt: str) -> str:
    if not OPENROUTER_KEY:
        raise ValueError("Missing OPENROUTER_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "HTTP-Referer": "https://github.com/your/repo",
                "X-Title": "Agri-Bot",
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def ask_hf(prompt: str) -> str:
    if not HF_KEY:
        raise ValueError("Missing HUGGINGFACE_API_TOKEN")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api-inference.huggingface.co/models/facebook/blenderbot-400M-distill",
            headers={"Authorization": f"Bearer {HF_KEY}"},
            json={"inputs": prompt},
        )
        r.raise_for_status()
        data = r.json()
        return data[0]["generated_text"].strip()

async def ask_gemini(prompt: str) -> str:
    if not GEMINI_KEY:
        raise ValueError("Missing GEMINI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

async def get_ai_advice(prompt: str) -> str:
    for fn in [ask_openai, ask_openrouter, ask_gemini, ask_hf]:
        try:
            return await fn(prompt)
        except Exception as e:
            logging.warning(f"AI provider failed: {e}")
    return "Xin lỗi, hiện tại hệ thống AI không khả dụng. Vui lòng thử lại sau hoặc cấu hình API key."

# ============= WEATHER (Next 6 hours) ==============
async def get_hourly_forecast(location: str, hours: int = 6):
    """
    Trả về danh sách forecast trong `hours` giờ tới.
    Mỗi item: {"hours_ahead": n, "time": "HH:MM dd-mm", "temp_c": X, "desc": "..."}
    Nếu không có OWM_KEY hoặc lỗi, trả về [].
    """
    if not OWM_KEY:
        logging.info("OWM_KEY not configured — skipping weather forecast.")
        return []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 1) Geocoding: lấy lat/lon từ tên địa điểm (limit=1)
            geo_url = "http://api.openweathermap.org/geo/1.0/direct"
            geo_params = {"q": location, "limit": 1, "appid": OWM_KEY}
            geo_r = await client.get(geo_url, params=geo_params)
            geo_r.raise_for_status()
            geo_data = geo_r.json()
            if not geo_data:
                logging.warning(f"Geocoding không tìm thấy toạ độ cho '{location}'")
                return []

            lat = geo_data[0]["lat"]
            lon = geo_data[0]["lon"]

            # 2) One Call API để lấy hourly
            onecall_url = "https://api.openweathermap.org/data/2.5/onecall"
            oc_params = {
                "lat": lat,
                "lon": lon,
                "exclude": "minutely,daily,alerts",
                "appid": OWM_KEY,
                "units": "metric",
            }
            oc_r = await client.get(onecall_url, params=oc_params)
            oc_r.raise_for_status()
            oc = oc_r.json()

            hourly = oc.get("hourly", [])
            timezone_offset = oc.get("timezone_offset", 0)  # seconds

            result = []
            for i in range(min(hours, len(hourly))):
                h = hourly[i]
                dt_unix = h.get("dt")
                # convert to local time with timezone_offset
                local_dt = datetime.utcfromtimestamp(dt_unix + timezone_offset)
                time_str = local_dt.strftime("%H:%M %d-%m")
                temp_c = h.get("temp")
                weather_desc = ""
                if h.get("weather"):
                    weather_desc = h["weather"][0].get("description", "")
                result.append({
                    "hours_ahead": i + 1,
                    "time": time_str,
                    "temp_c": temp_c,
                    "desc": weather_desc
                })

            return result

    except Exception as e:
        logging.error(f"Lỗi khi lấy forecast từ OWM: {e}")
        return []

# ============= THINGSBOARD ==============
def push_to_tb(data: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard: {data}")
    except Exception as e:
        logging.error(f"❌ Failed to push telemetry: {e}")

# ============= ENDPOINTS ==============
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    logging.info(f"📩 Got TB webhook: {body}")

    shared = body.get("shared", {})
    hoi = shared.get("hoi", "Hãy đưa ra lời khuyên nông nghiệp.")
    crop = shared.get("crop", "cây trồng")
    location = shared.get("location", "Hồ Chí Minh")

    prompt = f"""
    Người dùng hỏi: {hoi}
    Cây trồng: {crop}
    Vị trí: {location}
    Hãy trả lời ngắn gọn, thực tế, dễ hiểu cho nông dân. Chỉ cần đưa ra 1 đoạn văn duy nhất.
    """

    advice_text = await get_ai_advice(prompt)

    # Lấy forecast 6 giờ tiếp theo (nếu có)
    forecast = await get_hourly_forecast(location, hours=6)

    # Chuẩn bị payload để push lên ThingsBoard
    payload = {"advice_text": advice_text}

    # Thêm các trường '1_gio_tiep_theo', '2_gio_tiep_theo', ...
    # format: "HH:MM dd-mm — 29.3°C — nhẹ mưa"
    for item in forecast:
        n = item["hours_ahead"]
        key = f"{n}_gio_tiep_theo"
        friendly = f'{item["time"]} — {round(item["temp_c"],1)}°C'
        if item.get("desc"):
            friendly += f' — {item["desc"]}'
        payload[key] = friendly

    # Nếu muốn đẩy cả danh sách chi tiết, có thể thêm:
    if forecast:
        payload["hourly_forecast"] = forecast

    push_to_tb(payload)

    # Trả response cho ThingsBoard webhook caller
    return {"status": "ok", "advice_text": advice_text, "forecast": forecast}

@app.get("/")
def root():
    return {"status": "running"}

# ============= STARTUP ==============
@app.on_event("startup")
def init():
    logging.info("🚀 Agri-Bot AI service started, waiting for ThingsBoard...")
