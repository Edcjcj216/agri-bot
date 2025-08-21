import os
import json
import logging
import requests
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

AI_API_URL = os.getenv("AI_API_URL", "https://api.openai.com/v1/chat/completions")
AI_TOKEN = os.getenv("AI_TOKEN")  # OpenAI API Key
TB_TOKEN = os.getenv("TB_TOKEN")  # ThingsBoard Device Token
TB_URL = os.getenv("TB_URL", "https://demo.thingsboard.io")  # Thay bằng URL thật nếu dùng TB riêng

app = FastAPI()

# --- Weather code mapping ---
WX_MAP = {
    0: "Trời quang",
    1: "Chủ yếu quang",
    2: "Ít mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    56: "Mưa phùn nhẹ băng",
    57: "Mưa phùn dày băng",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa băng nhẹ",
    67: "Mưa băng to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết to",
    77: "Tuyết hạt",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    85: "Mưa tuyết nhẹ",
    86: "Mưa tuyết to",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá to",
}

# --- Helper: fetch weather data from Open-Meteo ---
def get_weather(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
        f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
        f"&forecast_days=2&timezone=Asia/Ho_Chi_Minh"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    # Current weather = giờ gần nhất
    now_idx = 0
    now = {
        "temp": data["hourly"]["temperature_2m"][now_idx],
        "humidity": data["hourly"]["relative_humidity_2m"][now_idx],
        "desc": WX_MAP.get(data["hourly"]["weathercode"][now_idx], "Không rõ"),
        "iso": data["hourly"]["time"][now_idx],
    }

    # 6 giờ tiếp theo
    next_hours = []
    for i in range(1, 7):
        next_hours.append({
            "hour": int(data["hourly"]["time"][i].split("T")[1].split(":")[0]),
            "temp": data["hourly"]["temperature_2m"][i],
            "humidity": data["hourly"]["relative_humidity_2m"][i],
            "desc": WX_MAP.get(data["hourly"]["weathercode"][i], "Không rõ"),
            "iso": data["hourly"]["time"][i],
        })

    # Hôm nay và ngày mai
    hom_nay = {
        "temp_min": data["daily"]["temperature_2m_min"][0],
        "temp_max": data["daily"]["temperature_2m_max"][0],
        "desc": WX_MAP.get(data["daily"]["weathercode"][0], "Không rõ"),
    }
    ngay_mai = {
        "temp_min": data["daily"]["temperature_2m_min"][1],
        "temp_max": data["daily"]["temperature_2m_max"][1],
        "desc": WX_MAP.get(data["daily"]["weathercode"][1], "Không rõ"),
    }

    return now, next_hours, hom_nay, ngay_mai

# --- Helper: call AI ---
def call_ai_api(sensor_text, crop, lat, lon, ngay_mai):
    if not AI_TOKEN:
        logger.error("AI_TOKEN chưa được cấu hình! Bỏ qua gọi AI.")
        return {
            "advice": "Thiếu AI token",
            "advice_nutrition": "",
            "advice_care": "",
            "advice_note": ""
        }

    headers = {
        "Authorization": f"Bearer {AI_TOKEN}",
        "Content-Type": "application/json",
    }
    prompt = (
        f"Dữ liệu cảm biến: {sensor_text}. "
        f"Cây: {crop} tại {lat:.5f},{lon:.5f}. "
        f"Ngày mai: {ngay_mai['desc']}, {ngay_mai['temp_min']}–{ngay_mai['temp_max']}°C. "
        f"Viết 1 câu dự báo và 1 câu gợi ý chăm sóc dinh dưỡng, 1 câu lưu ý quan sát."
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }

    logger.info("AI ▶ Prompt gửi đi: %s", prompt)
    r = requests.post(AI_API_URL, headers=headers, data=json.dumps(payload), timeout=20)
    if r.status_code != 200:
        logger.error("AI ◀ Lỗi status=%s body=%s", r.status_code, r.text)
        return {
            "advice": "AI API lỗi",
            "advice_nutrition": "",
            "advice_care": "",
            "advice_note": ""
        }

    res = r.json()
    content = res["choices"][0]["message"]["content"]
    logger.info("AI ◀ Phản hồi: %s", content)
    parts = content.split("|")
    return {
        "advice": content,
        "advice_nutrition": parts[0].strip() if len(parts) > 0 else "",
        "advice_care": parts[1].strip() if len(parts) > 1 else "",
        "advice_note": parts[2].strip() if len(parts) > 2 else ""
    }

# --- Helper: push telemetry ---
def push_telemetry(payload):
    if not TB_TOKEN:
        logger.error("TB_TOKEN chưa được cấu hình! Bỏ qua gửi ThingsBoard.")
        return
    url = f"{TB_URL}/api/v1/{TB_TOKEN}/telemetry"
    logger.info("Telemetry ▶ %s", json.dumps(payload, ensure_ascii=False))
    r = requests.post(url, json=payload, timeout=10)
    logger.info("Telemetry ◀ status=%s body=%s", r.status_code, r.text)

# --- Auto loop trigger ---
def auto_loop(data):
    lat = data.get("lat", 10.80609)
    lon = data.get("lon", 106.75222)
    crop = data.get("crop", "Không rõ")
    temperature = data.get("temperature")
    humidity = data.get("humidity")
    battery = data.get("battery")

    # Lấy thời tiết
    now, next_hours, hom_nay, ngay_mai = get_weather(lat, lon)
    sensor_text = f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%, pin {battery}%"
    ai_advice = call_ai_api(sensor_text, crop, lat, lon, ngay_mai)

    # Build telemetry
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "temperature": temperature,
        "humidity": humidity,
        "battery": battery,
        "crop": crop,
        "location": f"{lat:.5f},{lon:.5f}",
        "wx_hien_tai": now,
        "wx_ngay_mai": ngay_mai,
        "hom_nay": hom_nay,
        "ngay_mai": ngay_mai,
        "prediction": sensor_text,
        "advice": ai_advice["advice"],
        "advice_nutrition": ai_advice["advice_nutrition"],
        "advice_care": ai_advice["advice_care"],
        "advice_note": ai_advice["advice_note"]
    }
    # 6 giờ tiếp theo → tách từng key riêng
    for idx, h in enumerate(next_hours, start=1):
        payload[f"wx_hour_{idx}"] = h

    push_telemetry(payload)
    return ai_advice

# --- FastAPI endpoint ---
@app.post("/esp32-data")
async def esp32_data(request: Request):
    data = await request.json()
    ai_advice = auto_loop(data)
    return JSONResponse({"status": "ok", "ai_advice": ai_advice})

@app.get("/")
async def root():
    return {"status": "running"}
