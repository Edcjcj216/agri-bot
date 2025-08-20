# ================== main.py ==================
from fastapi import FastAPI, Request
import requests, json, os, asyncio

app = FastAPI()

# ================== CONFIG ==================
DEMO_DEVICE_TOKEN = "66dd31thvta4gx1l781q"
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1"

AI_API_URL = "https://api.example.com/predict"  # Thay bằng API thật
AI_API_KEY = os.getenv("AI_API_KEY")            # hoặc đặt trực tiếp

# ================== Bộ nhớ tạm lưu dữ liệu ESP32 mới nhất ==================
latest_data = {"temperature": None, "humidity": None}


# ================== Hàm gọi AI API và gửi kết quả lên ThingsBoard ==================
def process_and_send_ai():
    temperature = latest_data.get("temperature")
    humidity    = latest_data.get("humidity")

    if temperature is None or humidity is None:
        print("⚠️ Chưa có dữ liệu ESP32 → bỏ qua push")
        return

    # ---- Gọi AI API ----
    ai_payload = {"temperature": temperature, "humidity": humidity}
    try:
        ai_resp = requests.post(
            AI_API_URL,
            headers={"Authorization": f"Bearer {AI_API_KEY}"},
            json=ai_payload,
            timeout=10
        )
        ai_resp.raise_for_status()
        ai_result = ai_resp.json()
        prediction = ai_result.get("prediction", f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%")
        advice     = ai_result.get("advice", "Theo dõi cây trồng, tưới nước đều, bón phân cân đối")
    except Exception as e:
        print(f"❌ AI API error: {e}")
        prediction = f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"
        advice     = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"

    # ---- Gửi Telemetry lên DEMO device ----
    telemetry = {"prediction": prediction, "advice": advice}
    try:
        resp = requests.post(
            f"{THINGSBOARD_URL}/{DEMO_DEVICE_TOKEN}/telemetry",
            headers={"Content-Type": "application/json"},
            data=json.dumps(telemetry),
            timeout=5
        )
        resp.raise_for_status()
        print(f"✅ AI telemetry sent: {telemetry}")
    except Exception as e:
        print(f"❌ Error sending telemetry: {e}")


# ================== REST endpoint nhận dữ liệu ESP32 thật ==================
@app.post("/esp32-data")
async def receive_esp32(request: Request):
    data = await request.json()

    # Lưu lại dữ liệu mới nhất
    latest_data["temperature"] = data.get("temperature")
    latest_data["humidity"]    = data.get("humidity")

    # Push prediction ngay lập tức
    process_and_send_ai()

    return {"status": "ok", "latest_data": latest_data}


# ================== Background loop mỗi 5 phút ==================
async def ai_loop():
    while True:
        process_and_send_ai()
        await asyncio.sleep(300)  # 5 phút


# ================== Health check endpoint ==================
@app.get("/")
async def root():
    return {"status": "running", "message": "Render server ready"}


# ================== Khởi chạy server + AI loop ==================
if __name__ == "__main__":
    import threading
    import uvicorn

    loop = asyncio.get_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(ai_loop()), daemon=True).start()

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
