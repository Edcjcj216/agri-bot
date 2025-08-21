import os
import json
import logging
import requests
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== CONFIG =====
TB_TOKEN = os.getenv("TB_TOKEN", "YOUR_THINGSBOARD_TOKEN")
TB_URL = os.getenv("TB_URL", "https://demo.thingsboard.io")  # hoặc URL của TB của bạn
AI_URL = os.getenv("AI_URL", "https://api.openai.com/v1/responses")
AI_TOKEN = os.getenv("AI_TOKEN", "YOUR_AI_TOKEN")
LOCATION_NAME = "An Phú, Thành phố Hồ Chí Minh"  # Cố định, bỏ reverse-geocode

# ===== Helper: Push telemetry lên ThingsBoard =====
def push_telemetry(payload: dict):
    url = f"{TB_URL}/api/v1/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logging.info(f"Telemetry pushed: {payload}")
    except Exception as e:
        logging.warning(f"Push telemetry error: {e}")

# ===== Helper: Call AI API =====
def call_ai_api(sensor_text: str, weather_text: str, crop: str):
    if not AI_TOKEN or AI_TOKEN.strip() == "":
        logging.error("AI_TOKEN is missing! Please set env var AI_TOKEN.")
        return None

    # Log token (chỉ log 6 ký tự đầu cho an toàn)
    logging.info(f"Using AI token: {AI_TOKEN[:6]}******")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_TOKEN}"
    }
    prompt = (
        f"Dữ liệu cảm biến: {sensor_text}. "
        f"Cây: {crop} tại {LOCATION_NAME}. "
        f"{weather_text}. Viết 1 câu dự báo và 1 câu gợi ý chăm sóc."
    )
    body = {
        "model": "gpt-4.1-mini",
        "input": prompt
    }
    try:
        r = requests.post(AI_URL, headers=headers, json=body, timeout=15)
        logging.info(f"AI request status={r.status_code}")
        r.raise_for_status()
        data = r.json()
        if "output" in data and "content" in data["output"]:
            return data["output"]["content"][0]["text"]
        elif "choices" in data:  # fallback cho OpenAI cũ
            return data["choices"][0]["message"]["content"]
        else:
            logging.warning(f"Unexpected AI response: {data}")
            return None
    except Exception as e:
        logging.error(f"AI API error: {e}")
        return None

# ===== Helper: Weather API (Open-Meteo) =====
def get_weather(lat: float, lon: float):
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,weathercode"
            f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
            f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
            f"&forecast_days=2&timezone=auto&lang=vi"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        wx = r.json()

        # ---- Hiện tại ----
        cur_temp = wx["current"]["temperature_2m"]
        cur_hum = wx["current"]["relative_humidity_2m"]
        cur_desc = wx["current"]["weathercode"]  # mapping code -> desc bên dưới
        cur_iso = wx["current"]["time"]

        wx_hien_tai = {
            "temp": cur_temp,
            "humidity": cur_hum,
            "desc": weather_code_to_desc(cur_desc),
            "iso": cur_iso
        }

        # ---- 6 giờ tiếp theo ----
        hours = []
        now_index = wx["hourly"]["time"].index(cur_iso)
        for i in range(1, 7):
            idx = now_index + i
            hours.append({
                "hour": int(wx["hourly"]["time"][idx].split("T")[1].split(":")[0]),
                "temp": wx["hourly"]["temperature_2m"][idx],
                "humidity": wx["hourly"]["relative_humidity_2m"][idx],
                "desc": weather_code_to_desc(wx["hourly"]["weathercode"][idx]),
                "iso": wx["hourly"]["time"][idx]
            })

        # ---- Ngày mai ----
        wx_ngay_mai = {
            "temp_min": wx["daily"]["temperature_2m_min"][1],
            "temp_max": wx["daily"]["temperature_2m_max"][1],
            "desc": weather_code_to_desc(wx["daily"]["weathercode"][1])
        }

        return wx_hien_tai, hours, wx_ngay_mai

    except Exception as e:
        logging.error(f"Weather API error: {e}")
        return None, [], None

# ===== Helper: Map weather code to description =====
def weather_code_to_desc(code: int) -> str:
    mapping = {
        0: "Trời quang",
        1: "Ít mây",
        2: "Có mây",
        3: "Nhiều mây",
        45: "Sương mù",
        48: "Sương mù",
        51: "Mưa phùn nhẹ",
        53: "Mưa phùn",
        55: "Mưa phùn dày",
        61: "Mưa nhẹ",
        63: "Mưa vừa",
        65: "Mưa to",
        80: "Mưa rào nhẹ",
        81: "Mưa rào vừa",
        82: "Mưa rào to",
        95: "Giông nhẹ hoặc vừa",
        96: "Giông có mưa đá nhẹ",
        99: "Giông có mưa đá to"
    }
    return mapping.get(code, "Không rõ")

# ===== API nhận dữ liệu từ ESP32 =====
@app.post("/esp32-data")
async def receive_data(request: Request):
    data = await request.json()
    temp = data.get("temperature")
    hum = data.get("humidity")
    bat = data.get("battery", 100)
    crop = data.get("crop", "Rau muống")
    lat = data.get("lat", 10.80609)
    lon = data.get("lon", 106.75222)

    # Lấy dữ liệu thời tiết
    wx_hien_tai, hours, wx_ngay_mai = get_weather(lat, lon)

    # Chuẩn bị mô tả cảm biến + thời tiết cho AI
    sensor_text = f"Nhiệt độ {temp}°C, độ ẩm {hum}%, pin {bat}%"
    weather_text = f"Ngày mai: {wx_ngay_mai['desc']}, {wx_ngay_mai['temp_min']}–{wx_ngay_mai['temp_max']}°C" if wx_ngay_mai else ""

    ai_advice = call_ai_api(sensor_text, weather_text, crop)

    # Push telemetry
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "temperature": temp,
        "humidity": hum,
        "battery": bat,
        "crop": crop,
        "location": f"{lat},{lon}",
        "location_name": LOCATION_NAME,
        "wx_hien_tai": wx_hien_tai,
        "wx_ngay_mai": wx_ngay_mai,
        "advice": ai_advice or "",
    }

    # Tách 6 giờ thành 6 key riêng
    for i, h in enumerate(hours, start=1):
        payload[f"wx_hour_{i}"] = h

    push_telemetry(payload)
    return JSONResponse({"status": "ok", "ai_advice": ai_advice})

# ===== Auto loop có thể chạy nếu cần =====
# Bỏ qua nếu ESP32 push chủ động

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
