import os
import json
import logging
from fastapi import FastAPI, Request
import httpx
import asyncio
import uvicorn
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI()

# --- Config ---
AI_TOKEN = os.getenv("AI_TOKEN", "YOUR_AI_TOKEN_HERE")
TB_TOKEN = "pk94asonfacs6mbeuutg"   # luôn dùng token cố định
TB_URL = "https://demo.thingsboard.io"

# In ra token để debug
logging.info(f"AI_TOKEN = {AI_TOKEN}")
logging.info(f"TB_TOKEN = {TB_TOKEN}")

# --- Hàm gọi AI ---
async def call_ai_api(sensor_data: dict, weather_today: dict, weather_tomorrow: dict) -> dict:
    if not AI_TOKEN or AI_TOKEN == "YOUR_AI_TOKEN_HERE":
        logging.warning("AI_TOKEN chưa được cấu hình!")
        return {
            "advice": "Không thể sinh gợi ý (chưa có AI_TOKEN).",
            "advice_nutrition": "",
            "advice_care": "",
            "advice_note": ""
        }

    url = "https://api.your-ai-service.com/advice"  # thay bằng endpoint thực tế
    payload = {
        "temperature": sensor_data.get("temperature"),
        "humidity": sensor_data.get("humidity"),
        "battery": sensor_data.get("battery"),
        "weather_today": weather_today,
        "weather_tomorrow": weather_tomorrow,
        "plant": "Rau muống",
        "location": "An Phú, Thành phố Hồ Chí Minh"
    }
    headers = {"Authorization": f"Bearer {AI_TOKEN}"}

    logging.info(f"AI ▶ {payload}")

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            logging.info(f"AI ◀ status={resp.status_code}")
            if resp.status_code != 200:
                logging.warning(f"AI API returned {resp.status_code}: {resp.text}")
                return {
                    "advice": "Không thể sinh gợi ý (AI lỗi).",
                    "advice_nutrition": "",
                    "advice_care": "",
                    "advice_note": ""
                }
            return resp.json()
        except Exception as e:
            logging.error(f"AI API exception: {e}")
            return {
                "advice": "Không thể sinh gợi ý (AI exception).",
                "advice_nutrition": "",
                "advice_care": "",
                "advice_note": ""
            }

# --- Hàm gửi dữ liệu lên ThingsBoard ---
async def send_to_tb(telemetry: dict):
    url = f"{TB_URL}/api/v1/{TB_TOKEN}/telemetry"
    logging.info(f"TB ▶ POST {url}")
    logging.info(f"TB ▶ Payload = {telemetry}")
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(url, json=telemetry)
            if resp.status_code != 200:
                logging.warning(f"TB API returned {resp.status_code}: {resp.text}")
            else:
                logging.info(f"TB ◀ OK {resp.status_code}")
        except Exception as e:
            logging.error(f"TB API exception: {e}")

# --- API nhận dữ liệu từ ESP32 ---
@app.post("/esp32-data")
async def receive_data(request: Request):
    data = await request.json()
    temperature = float(data.get("temperature", 0))
    humidity = float(data.get("humidity", 0))
    battery = float(data.get("battery", 0))

    # TODO: Tích hợp dữ liệu thời tiết thực (6 giờ + ngày mai)
    weather_today = {"weather_desc": "Giông nhẹ hoặc vừa", "temp_max": 27.1, "temp_min": 24.4}
    weather_tomorrow = {"weather_desc": "Nhiều mây", "temp_max": 32.0, "temp_min": 23.5}

    sensor_data = {"temperature": temperature, "humidity": humidity, "battery": battery,
                   "prediction": f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"}

    advice = await call_ai_api(sensor_data, weather_today, weather_tomorrow)

    telemetry = {
        **sensor_data,
        **advice,
        "hom_nay": weather_today,
        "ngay_mai": weather_tomorrow
    }

    await send_to_tb(telemetry)
    return {"status": "ok", "data": telemetry}

# --- Trang chủ ---
@app.get("/")
def root():
    return {"message": "Service is running", "time": datetime.now().isoformat()}

# --- Auto loop nếu cần ---
async def auto_loop():
    while True:
        fake_data = {
            "temperature": 30.1,
            "humidity": 69.2,
            "battery": 90
        }
        await receive_data(type("obj", (object,), {"json": lambda: fake_data}))
        await asyncio.sleep(60)

# --- Khởi động ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    logging.info(f"Starting server on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
