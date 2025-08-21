import os
import json
import logging
from datetime import datetime, timedelta
import requests
import uvicorn
from fastapi import FastAPI, Request

# ===== Logging setup =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ===== FastAPI App =====
app = FastAPI()

# ===== ENV TOKEN =====
AI_TOKEN = os.getenv("AI_TOKEN", "")
TB_TOKEN = os.getenv("TB_TOKEN", "")

# ===== Weathercode map → tiếng Việt =====
WEATHER_CODE_MAP = {
    0: "Trời quang",
    1: "Ít mây",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù đông đá",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa đông đá nhẹ",
    67: "Mưa đông đá nặng",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    77: "Tuyết hạt",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    85: "Mưa tuyết nhẹ",
    86: "Mưa tuyết to",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông có mưa đá nhẹ",
    99: "Giông có mưa đá nặng"
}

# ===== Root health check =====
@app.get("/")
async def root():
    return {"status": "running"}

# ===== Lấy dự báo thời tiết từ Open-Meteo =====
def get_weather(lat: float, lon: float):
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,relative_humidity_2m,weathercode"
        "&daily=temperature_2m_max,temperature_2m_min,weathercode"
        "&forecast_days=2&timezone=Asia%2FBangkok"
    )
    logger.info("Fetching weather: %s", url)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    # --- Giờ hiện tại ---
    now_idx = 0
    now_temp = data["hourly"]["temperature_2m"][now_idx]
    now_humidity = data["hourly"]["relative_humidity_2m"][now_idx]
    now_code = data["hourly"]["weathercode"][now_idx]
    now_desc = WEATHER_CODE_MAP.get(now_code, "Không rõ")

    wx_hien_tai = {
        "temp": now_temp,
        "humidity": now_humidity,
        "desc": now_desc,
        "iso": data["hourly"]["time"][now_idx]
    }

    # --- 6 giờ tiếp theo ---
    wx_gio = []
    for i in range(1, 7):
        hour_time = data["hourly"]["time"][i]
        hour_temp = data["hourly"]["temperature_2m"][i]
        hour_hum = data["hourly"]["relative_humidity_2m"][i]
        hour_code = data["hourly"]["weathercode"][i]
        hour_desc = WEATHER_CODE_MAP.get(hour_code, "Không rõ")
        hour_obj = {
            "hour": int(hour_time.split("T")[1].split(":")[0]),
            "temp": hour_temp,
            "humidity": hour_hum,
            "desc": hour_desc,
            "iso": hour_time
        }
        wx_gio.append(hour_obj)

    # --- Thời tiết hôm nay + ngày mai ---
    wx_hom_nay = {
        "weather_desc": WEATHER_CODE_MAP.get(data["daily"]["weathercode"][0], "Không rõ"),
        "temp_max": data["daily"]["temperature_2m_max"][0],
        "temp_min": data["daily"]["temperature_2m_min"][0],
    }
    wx_ngay_mai = {
        "weather_desc": WEATHER_CODE_MAP.get(data["daily"]["weathercode"][1], "Không rõ"),
        "temp_max": data["daily"]["temperature_2m_max"][1],
        "temp_min": data["daily"]["temperature_2m_min"][1],
    }

    return wx_hien_tai, wx_gio, wx_hom_nay, wx_ngay_mai

# ===== Gọi AI API sinh gợi ý =====
def call_ai_api(sensor_text: str, crop: str, location: str, wx_ngay_mai: dict):
    if not AI_TOKEN:
        logger.warning("AI_TOKEN KHÔNG CÓ — bỏ qua gọi AI")
        return "Không có AI_TOKEN để sinh gợi ý."

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {AI_TOKEN}",
        "Content-Type": "application/json"
    }
    prompt = (
        f"Dữ liệu cảm biến: {sensor_text}. "
        f"Cây: {crop} tại {location}. "
        f"Ngày mai: {wx_ngay_mai['weather_desc']}, {wx_ngay_mai['temp_min']}–{wx_ngay_mai['temp_max']}°C. "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc cây bằng tiếng Việt."
    )
    payload = {
        "model": "gpt-4.1-mini",
        "input": prompt
    }
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["output_text"] if "output_text" in data else str(data)

# ===== Đẩy telemetry lên ThingsBoard =====
def post_telemetry(payload: dict):
    if not TB_TOKEN:
        logger.warning("TB_TOKEN KHÔNG CÓ — bỏ qua đẩy TB")
        return
    url = f"https://demo.thingsboard.io/api/v1/{TB_TOKEN}/telemetry"
    headers = {"Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    if r.status_code != 200:
        logger.warning("TB post lỗi %s: %s", r.status_code, r.text)

# ===== Endpoint nhận dữ liệu từ ESP32 =====
@app.post("/esp32-data")
async def receive_esp32_data(request: Request):
    data = await request.json()
    temp = data.get("temperature")
    hum = data.get("humidity")
    batt = data.get("battery")
    crop = data.get("crop", "Không rõ")
    lat = data.get("lat")
    lon = data.get("lon")

    logger.info("ESP32 data: %s", data)

    # --- Lấy thời tiết ---
    wx_hien_tai, wx_gio, wx_hom_nay, wx_ngay_mai = get_weather(lat, lon)

    # --- Gọi AI ---
    sensor_text = f"Nhiệt độ {temp}°C, độ ẩm {hum}%, pin {batt}%"
    location_str = f"{lat:.5f},{lon:.5f}"
    ai_resp = call_ai_api(sensor_text, crop, location_str, wx_ngay_mai)
    logger.info("AI ▶ %s", sensor_text)
    logger.info("AI ◀ %s", ai_resp)

    # --- Telemetry ThingsBoard ---
    tb_payload = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "temperature": temp,
        "humidity": hum,
        "battery": batt,
        "crop": crop,
        "location": location_str,
        "prediction": sensor_text,
        "advice": ai_resp,
        "wx_hien_tai": wx_hien_tai,
        "hom_nay": wx_hom_nay,
        "ngay_mai": wx_ngay_mai,
    }
    # tách 6 giờ tới thành key riêng
    for i, h in enumerate(wx_gio, start=1):
        tb_payload[f"wx_hour_{i}"] = h

    post_telemetry(tb_payload)

    return {"status": "ok", "ai": ai_resp, "weather": tb_payload}

# ===== On startup print tokens =====
@app.on_event("startup")
async def startup_event():
    logger.info("=== App khởi động ===")
    logger.info("AI_TOKEN: %s", AI_TOKEN if AI_TOKEN else "KHÔNG CÓ")
    logger.info("TB_TOKEN: %s", TB_TOKEN if TB_TOKEN else "KHÔNG CÓ")
    logger.info("PORT: %s", os.getenv("PORT", "8000 (mặc định)"))

# ===== Run uvicorn (for local or Render) =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
