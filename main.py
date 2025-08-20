# ================== main.py ==================
from fastapi import FastAPI, Request
import requests, json, os, asyncio

app = FastAPI()

# ================== CONFIG ==================
DEMO_DEVICE_TOKEN = "66dd31thvta4gx1l781q"
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1"

AI_API_URL = "https://api.example.com/predict"  # Thay bằng API thật
AI_API_KEY = os.getenv("AI_API_KEY")            # hoặc đặt trực tiếp

# ================== REST endpoint nhận dữ liệu ESP32 thật ==================
@app.post("/esp32-data")
async def receive_esp32(request: Request):
    data = await request.json()

    temperature = data.get("temperature")
    humidity    = data.get("humidity")

    # ================== Gọi AI API ==================
    ai_payload = {
        "temperature": temperature,
        "humidity": humidity
    }
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
    except Exception:
        prediction = f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"
        advice     = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"

    # ================== Telemetry chỉ gửi prediction + advice ==================
    telemetry = {
        "prediction": prediction,
        "advice": advice
    }

    try:
        resp = requests.post(
            f"{THINGSBOARD_URL}/{DEMO_DEVICE_TOKEN}/telemetry",
            headers={"Content-Type": "application/json"},
            data=json.dumps(telemetry),
            timeout=5
        )
        resp.raise_for_status()
    except Exception as e:
        return {"status": "fail", "error": str(e), "telemetry": telemetry}

    return {"status": "ok", "telemetry": telemetry}

# ================== Demo loop gửi dữ liệu AI giả mỗi 5 phút ==================
async def send_demo_loop():
    import random, time
    while True:
        temperature = round(random.uniform(25, 32),1)
        humidity    = round(random.uniform(50, 80),1)
        prediction  = f"Dữ liệu demo: nhiệt độ {temperature}°C, độ ẩm {humidity}%"
        advice      = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"
        telemetry = {
            "prediction": prediction,
            "advice": advice
        }
        try:
            requests.post(
                f"{THINGSBOARD_URL}/{DEMO_DEVICE_TOKEN}/telemetry",
                headers={"Content-Type": "application/json"},
                data=json.dumps(telemetry),
                timeout=5
            )
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Demo telemetry sent")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ❌ Error: {e}")
        await asyncio.sleep(300)  # 5 phút

# ================== Khởi chạy server + demo loop ==================
if __name__ == "__main__":
    import threading
    import uvicorn

    loop = asyncio.get_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(send_demo_loop()), daemon=True).start()

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
