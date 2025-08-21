import os
import time
import json
import logging
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ================== CONFIG ==================
TB_DEMO_TOKEN = "pk94asonfacs6mbeuutg"  # Device DEMO token mới
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

TB_TENANT_USER = os.getenv("TB_TENANT_USER", "")
TB_TENANT_PASS = os.getenv("TB_TENANT_PASS", "")

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== HELPERS ==================
def call_ai_api(data: dict) -> dict:
    """Gọi AI (nếu có) rồi trả về luôn các trường:
    - prediction: tóm tắt số
    - advice: văn bản lời khuyên (AI nếu có, else local)
    - advice_nutrition: phần dinh dưỡng ngắn
    - advice_care: phần chăm sóc ngắn
    - advice_note: lưu ý/ngắn gọn

    Luôn đảm bảo các trường trên không rỗng (dùng quy tắc local nếu AI không trả).
    """
    model_url = AI_API_URL
    hf_token = HF_TOKEN
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    prompt = (
        f"Dự báo: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}% tại Hồ Chí Minh, cây Rau muống. "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_sections(temp: float, humi: float, battery: float | None = None) -> dict:
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        # Nutrition section
        nutrition = []
        nutrition.append("**Dinh dưỡng:** - Ưu tiên Kali (K)")
        nutrition.append("- Cân bằng NPK")
        nutrition.append("- Bón phân hữu cơ")
        nutrition.append("- Phân bón lá nếu cần")
        # Care section
        care = []
        # temperature-based
        if temp >= 35:
            care.append("- Tránh phơi nắng gắt; che chắn, tưới sáng sớm/chiều mát")
        elif temp >= 30:
            care.append("- Tưới đủ nước và theo dõi thường xuyên")
        elif temp <= 15:
            care.append("- Giữ ấm, tránh sương muối")
        else:
            care.append("- Nhiệt độ trong ngưỡng bình thường")
        # humidity-based
        if humi <= 40:
            care.append("- Độ ẩm thấp: tăng tưới")
        elif humi <= 60:
            care.append("- Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85:
            care.append("- Độ ẩm cao: kiểm tra thoát nước, tránh úng")
        else:
            care.append("- Độ ẩm ổn định cho rau muống")
        # other
        if battery is not None and battery <= 20:
            care.append("- Pin thiết bị thấp: kiểm tra nguồn/ắc quy")

        note = "**Lưu ý:** Quan sát cây trồng và điều chỉnh theo thực tế"

        return {
            "prediction": pred,
            "advice_nutrition": " ".join(nutrition),
            "advice_care": " ".join(care),
            "advice_note": note,
        }

    # Try AI call first; if success, use AI text as 'advice' but still build structured sections from local rules
    try:
        logger.info(f"AI ▶ POST {model_url} body={prompt[:200]}")
        r = requests.post(model_url, headers=headers, json=body, timeout=30)
        logger.info(f"AI ◀ status={r.status_code} text={(r.text or '')[:400]}")
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out:
                first = out[0]
                if isinstance(first, dict):
                    text = first.get("generated_text") or first.get("text") or str(first)
                else:
                    text = str(first)
            elif isinstance(out, dict):
                text = out.get("generated_text") or out.get("text") or json.dumps(out, ensure_ascii=False)
            else:
                text = str(out)

            sections = local_sections(data['temperature'], data['humidity'], data.get('battery'))
            if text:
                return {
                    "prediction": sections['prediction'],
                    "advice": text.strip(),
                    "advice_nutrition": sections['advice_nutrition'],
                    "advice_care": sections['advice_care'],
                    "advice_note": sections['advice_note'],
                }
            else:
                logger.warning("AI returned empty text, falling back to local sections")
    except Exception as e:
        logger.warning(f"AI API call failed: {e}, using local fallback")

    # Fallback: always return local structured sections + combined advice
    sec = local_sections(data['temperature'], data['humidity'], data.get('battery'))
    combined_advice = f"{sec['advice_nutrition']} {sec['advice_care']} {sec['advice_note']}"
    sec['advice'] = combined_advice
    return sec


def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {data}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ status={r.status_code} text={r.text}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== API ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    merged = data.dict() | ai_result
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

@app.get("/debug/test-push")
def test_push():
    sample = {"temperature": 29.5, "humidity": 72, "battery": 95,
              "prediction": "Nhiệt độ 29.5°C, độ ẩm 72%",
              "advice": "Tưới thêm nước nhẹ buổi sáng."}
    send_to_thingsboard(sample)
    return {"test": "pushed", "data": sample}

@app.get("/last-telemetry")
def last_telemetry(keys: str = "temperature,humidity,battery,prediction,advice"):
    if not TB_TENANT_USER or not TB_TENANT_PASS:
        return JSONResponse(status_code=400, content={"error": "Missing TB_TENANT_USER/PASS env vars"})

    try:
        # 1. Login tenant
        login_url = "https://thingsboard.cloud/api/auth/login"
        r = requests.post(login_url, json={"username": TB_TENANT_USER, "password": TB_TENANT_PASS}, timeout=10)
        r.raise_for_status()
        token = r.json()["token"]
        headers = {"X-Authorization": f"Bearer {token}"}

        # 2. Get deviceId by token
        device_url = f"https://thingsboard.cloud/api/device/token/{TB_DEMO_TOKEN}"
        r = requests.get(device_url, headers=headers, timeout=10)
        r.raise_for_status()
        device_id = r.json()["id"]["id"]

        # 3. Get latest telemetry
        ts_url = f"https://thingsboard.cloud/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries?keys={keys}"
        r = requests.get(ts_url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Last telemetry error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# ================== AUTO LOOP (5min) ==================
import threading

def auto_loop():
    while True:
        try:
            # Fake ESP32 data (có thể thay bằng đọc queue/thật)
            sample = {"temperature": 30.1, "humidity": 69.2, "battery": 90}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            ai_result = call_ai_api(sample)
            merged = sample | ai_result
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_loop, daemon=True).start()
