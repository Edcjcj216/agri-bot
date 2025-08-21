from fastapi import FastAPI, Request
import requests
import logging
import threading
import time

app = FastAPI()

# --- Config ---
THINGSBOARD_URL = "https://thingsboard.cloud"
DEMO_TOKEN = "pk94asonfacs6mbeuutg"   # token của DEMO device
AI_API_URL = "https://agri-bot-fc6r.onrender.com/fake-ai"  # API AI (fake trong server này)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Biến toàn cục lưu dữ liệu ESP32 ---
last_esp32_data = {"temperature": 0, "humidity": 0, "battery": 0}

# --- Fake AI API xử lý dữ liệu ---
@app.post("/fake-ai")
async def fake_ai(request: Request):
    data = await request.json()
    temp = data.get("temperature", 0)
    hum = data.get("humidity", 0)

    prediction = f"Nhiệt độ {temp}°C, độ ẩm {hum}%"
    advice_nutrition = "Dinh dưỡng: Ưu tiên Kali (K), cân bằng NPK, bón phân hữu cơ, phân bón lá"
    advice_care = "Chăm sóc: Tưới nước đều, làm cỏ, phòng trừ sâu bệnh, cắt tỉa, che chắn nắng"
    advice_note = "Ghi chú: Quan sát cây trồng và điều chỉnh theo thực tế"

    return {
        "prediction": prediction,
        "advice_nutrition": advice_nutrition,
        "advice_care": advice_care,
        "advice_note": advice_note
    }

# --- Endpoint nhận dữ liệu ESP32 ---
@app.post("/esp32-data")
async def receive_esp32_data(request: Request):
    global last_esp32_data
    data = await request.json()
    logging.info(f"✅ Nhận dữ liệu từ ESP32: {data}")

    last_esp32_data = data  # lưu lại dữ liệu mới nhất
    await call_ai_and_forward(data)
    return {"status": "ok", "received": data}

# --- Hàm gọi AI API + gửi lên ThingsBoard ---
async def call_ai_and_forward(data):
    try:
        logging.info("📡 Gọi AI API...")
        ai_resp = requests.post(AI_API_URL, json=data, timeout=10)
        ai_result = ai_resp.json()
        logging.info(f"🤖 AI trả về: {ai_result}")

        payload = {
            "prediction": ai_result["prediction"],
            "advice_nutrition": ai_result["advice_nutrition"],
            "advice_care": ai_result["advice_care"],
            "advice_note": ai_result["advice_note"]
        }

        url = f"{THINGSBOARD_URL}/api/v1/{DEMO_TOKEN}/telemetry"
        tb_resp = requests.post(url, json=payload, timeout=10)

        if tb_resp.status_code == 200:
            logging.info("✅ Đã gửi dữ liệu AI lên DEMO device")
        else:
            logging.error(f"❌ Lỗi gửi lên ThingsBoard: {tb_resp.text}")

    except Exception as e:
        logging.error(f"❌ call_ai_and_forward error: {e}")

# --- Hàm background: 5 phút gửi 1 lần ---
def auto_forward():
    while True:
        try:
            if last_esp32_data["temperature"] == 0:  
                # Nếu ESP32 chưa gửi thì dùng dummy test data
                dummy = {"temperature": 29.5, "humidity": 70.2, "battery": 85}
                logging.warning("⚠️ ESP32 chưa gửi dữ liệu → dùng dummy test data")
                requests.post("http://0.0.0.0:10000/esp32-data", json=dummy)
            else:
                # Gửi lại dữ liệu ESP32 thật
                requests.post("http://0.0.0.0:10000/esp32-data", json=last_esp32_data)

        except Exception as e:
            logging.error(f"❌ auto_forward error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_forward, daemon=True).start()

# --- Root check ---
@app.get("/")
async def root():
    return {"status": "running", "hint": "POST dữ liệu vào /esp32-data để test"}
