import os
import json
import logging
from datetime import datetime
import httpx
from fastapi import FastAPI, BackgroundTasks
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TB_TOKEN = os.getenv("TB_TOKEN")  # ThingsBoard device token
TB_URL = "https://thingsboard.cloud/api/v1"

LOCATION = "An Phú, Hồ Chí Minh"
LAT, LON = 10.8781, 106.7594  # Coordinates for OpenWeather API

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ================== FastAPI ==================
app = FastAPI(title="OpenWeather + AI + ThingsBoard")

# ================== THINGSBOARD ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        logging.info(f"Pushing telemetry: {json.dumps(payload, ensure_ascii=False)}")
        response = httpx.post(url, json=payload, timeout=10)
        logging.info(f"Response status: {response.status_code}, body: {response.text}")
        return response
    except Exception as e:
        logging.error(f"Error pushing telemetry: {e}")
        return None

# ================== OPENWEATHER ==================
def fetch_openweather():
    url = f"https://api.openweathermap.org/data/2.5/onecall"
    params = {
        "lat": LAT,
        "lon": LON,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "exclude": "minutely,daily,alerts"
    }
    try:
        r = httpx.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data
    except Exception as e:
        logging.error(f"Error fetching OpenWeather: {e}")
        return None

# ================== AI ADVICE ==================
async def get_ai_advice(temperature, humidity, weather_today, weather_tomorrow, weather_yesterday):
    system_prompt = (
        "Bạn là AI nông nghiệp. Dựa trên dữ liệu thời tiết hiện tại và dự báo, "
        "sinh ra gợi ý chăm sóc cây rau muống. Tối ưu câu ngắn gọn, phân tách advice thành: "
        "advice_nutrition, advice_care, advice_note."
    )
    user_prompt = json.dumps({
        "temperature": temperature,
        "humidity": humidity,
        "weather_today": weather_today,
        "weather_tomorrow": weather_tomorrow,
        "weather_yesterday": weather_yesterday
    })

    headers = {}
    data = {}
    # Prefer OpenRouter if key exists
    if OPENROUTER_API_KEY:
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
        data = {"prompt": f"{system_prompt}\n{user_prompt}", "max_tokens": 300}
        url = "https://openrouter.ai/api/v1/chat/completions"
    elif GEMINI_API_KEY:
        headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
        data = {"prompt": f"{system_prompt}\n{user_prompt}", "max_output_tokens": 300}
        url = "https://gemini.googleapis.com/v1/models/text-bison-001:generate"
    else:
        logging.warning("No AI API key found, returning default advice")
        return {
            "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
            "advice_care": "Nhiệt độ trong ngưỡng an toàn | Độ ẩm ổn định cho rau",
            "advice_note": "Quan sát thực tế và điều chỉnh"
        }

    try:
        r = httpx.post(url, headers=headers, json=data, timeout=15)
        r.raise_for_status()
        result = r.json()
        # OpenRouter/Gemini response parsing
        if "choices" in result:
            text = result["choices"][0]["message"]["content"]
        elif "candidates" in result:
            text = result["candidates"][0]["content"]
        else:
            text = str(result)
        logging.info(f"AI response: {text}")
        # Nếu AI trả về JSON, parse, nếu không trả về default
        try:
            advice_json = json.loads(text)
            return advice_json
        except:
            return {
                "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
                "advice_care": f"Thông tin AI: {text}",
                "advice_note": "Quan sát thực tế và điều chỉnh"
            }
    except Exception as e:
        logging.error(f"AI request failed: {e}")
        return {
            "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
            "advice_care": "Nhiệt độ trong ngưỡng an toàn | Độ ẩm ổn định cho rau",
            "advice_note": "Quan sát thực tế và điều chỉnh"
        }

# ================== TELEMETRY GENERATOR ==================
async def generate_and_push_telemetry():
    weather_data = fetch_openweather()
    if not weather_data:
        return

    now = datetime.now()
    hourly = weather_data.get("hourly", [])
    telemetry = {}

    # Lấy 7 giờ tiếp theo
    for i in range(min(7, len(hourly))):
        h = hourly[i]
        telemetry[f"hour_{i}"] = datetime.fromtimestamp(h["dt"]).strftime("%H:%M")
        telemetry[f"hour_{i}_temperature"] = round(h["temp"], 1)
        telemetry[f"hour_{i}_humidity"] = round(h["humidity"], 1)
        weather_desc = h.get("weather", [{}])[0].get("description", "Không rõ")
        telemetry[f"hour_{i}_weather_desc"] = weather_desc

    telemetry["temperature"] = round(hourly[0]["temp"], 1)
    telemetry["humidity"] = round(hourly[0]["humidity"], 1)
    telemetry["weather_today_desc"] = hourly[0].get("weather", [{}])[0].get("description", "Không rõ")
    telemetry["weather_today_max"] = round(max(h["temp"] for h in hourly[:12]), 1)
    telemetry["weather_today_min"] = round(min(h["temp"] for h in hourly[:12]), 1)

    telemetry["weather_tomorrow_desc"] = hourly[12].get("weather", [{}])[0].get("description", "Không rõ") if len(hourly) > 12 else telemetry["weather_today_desc"]
    telemetry["weather_tomorrow_max"] = round(max(h["temp"] for h in hourly[12:24]), 1) if len(hourly) > 12 else telemetry["weather_today_max"]
    telemetry["weather_tomorrow_min"] = round(min(h["temp"] for h in hourly[12:24]), 1) if len(hourly) > 12 else telemetry["weather_today_min"]
    telemetry["weather_yesterday_desc"] = "Không rõ"
    telemetry["weather_yesterday_max"] = telemetry["weather_today_max"]
    telemetry["weather_yesterday_min"] = telemetry["weather_today_min"]

    # Gọi AI sinh advice
    advice = await get_ai_advice(
        temperature=telemetry["temperature"],
        humidity=telemetry["humidity"],
        weather_today=telemetry["weather_today_desc"],
        weather_tomorrow=telemetry["weather_tomorrow_desc"],
        weather_yesterday=telemetry["weather_yesterday_desc"]
    )
    telemetry.update(advice)
    telemetry["location"] = LOCATION
    telemetry["crop"] = "Rau muống"
    telemetry["battery"] = round(4.18, 3)

    push_telemetry(telemetry)

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: httpx.get("http://localhost:10000/ping"), "interval", minutes=5)
scheduler.add_job(lambda: asyncio.run(generate_and_push_telemetry()), "interval", minutes=5)
scheduler.start()

# ================== ROUTES ==================
@app.get("/ping")
async def ping():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.on_event("startup")
async def startup_event():
    logging.info("Starting app, sending startup ping to ThingsBoard")
    push_telemetry({"startup_ping": datetime.now().isoformat()})
    await generate_and_push_telemetry()
