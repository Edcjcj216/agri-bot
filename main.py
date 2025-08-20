import json
import time
import requests
import random
import os

# =========================
# Cấu hình DEMO device
# =========================
DEMO_TOKEN = "kfj6183wtsdijxu3z4yx"  # Token DEMO device
TB_URL = f"https://thingsboard.cloud/api/v1/{DEMO_TOKEN}/telemetry"

CROP = "Rau muống"
LOCATION = "Ho Chi Minh,VN"

HF_API_KEY = os.getenv("HF_API_KEY")           # token Hugging Face
HF_MODEL = os.getenv("HF_MODEL", "google/flan-t5-small")

# =========================
# Hàm gọi Hugging Face
# =========================
def call_huggingface(prompt, timeout=30):
    if not HF_API_KEY:
        raise RuntimeError("HF_API_KEY chưa set")
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}", "Content-Type": "application/json"}
    body = {"inputs": prompt, "options": {"wait_for_model": True}}
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    out = resp.json()
    # parse linh hoạt
    if isinstance(out, list) and len(out) > 0:
        first = out[0]
        if isinstance(first, dict):
            return first.get("generated_text") or first.get("text") or str(first)
        return str(first)
    if isinstance(out, dict):
        return out.get("generated_text") or out.get("text") or str(out)
    return str(out)

# =========================
# Hàm AI logic động (HF)
# =========================
def run_ai_logic(sensor_data):
    nhiet_do = sensor_data["temperature_h"]
    do_am = sensor_data["humidity"]

    prompt = f"Dự báo nông nghiệp: nhiệt độ {nhiet_do}°C, độ ẩm {do_am}% tại {LOCATION}, cây {CROP}. Viết 1 prediction ngắn và 1 advice ngắn gọn."
    
    try:
        text = call_huggingface(prompt)
    except Exception as e:
        print(f"⚠️ HF AI lỗi, fallback cứng: {e}")
        text = f"Với nhiệt độ {nhiet_do}°C và độ ẩm {do_am}% tại {LOCATION}, cây {CROP} bình thường. Theo dõi nước và dinh dưỡng."

    # Có thể tách thành prediction/advice nếu muốn
    prediction = f"Nhiệt độ {nhiet_do}°C, độ ẩm {do_am}%"
    advice = text
    return prediction, advice

# =========================
# Hàm gửi dữ liệu
# =========================
def send_to_demo(sensor_data, prediction, advice):
    payload = {
        "temperature_h": sensor_data["temperature_h"],
        "humidity": sensor_data["humidity"],
        "battery": sensor_data["battery"],
        "crop": CROP,
        "location": LOCATION,
        "prediction": prediction,
        "advice": advice
    }
    try:
        r = requests.post(TB_URL, json=payload)
        r.raise_for_status()
        print(f"✅ [{time.strftime('%H:%M:%S')}] Gửi DEMO device: {payload}")
    except Exception as e:
        print(f"❌ Lỗi gửi DEMO device: {e}")

# =========================
# ESP32 ảo
# =========================
def fake_esp32_data():
    return {
        "temperature_h": round(random.uniform(24, 32), 1),
        "humidity": round(random.uniform(50, 80), 1),
        "battery": round(random.uniform(3.8, 4.2), 2)
    }

# =========================
# Vòng lặp chính
# =========================
i = 1
try:
    while True:
        sensor_data = fake_esp32_data()
        print(f"📥 ESP32 ảo gửi #{i}: {sensor_data}")

        prediction, advice = run_ai_logic(sensor_data)
        send_to_demo(sensor_data, prediction, advice)

        i += 1
        time.sleep(300)  # 5 phút
except KeyboardInterrupt:
    print("⏹️ Dừng demo")
