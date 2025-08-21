import os
import time
import logging
import requests
import threading
from fastapi import FastAPI
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "I1s5bI2FQCZw6umLvwLG")
TB_DEVICE_PUSH_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
LOOP_INTERVAL = 300  # 5 phút

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()
_last_payload = None

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang", 1: "Trời quang nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Giông nhẹ hoặc vừa", 96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

def get_weather_6points():
    """Lấy 6 mốc giờ liên tiếp từ Open-Meteo"""
    try:
        now = datetime.now()
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,relativehumidity_2m,weathercode",
            "timezone": "Asia/Ho_Chi_Minh"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        times = data.get("hourly", {}).get("time", [])
        temps = data.get("hourly", {}).get("temperature_2m", [])
        hums = data.get("hourly", {}).get("relativehumidity_2m", [])
        codes = data.get("hourly", {}).get("weathercode", [])

        points = []
        for i in range(6):
            target_time = now + timedelta(hours=i*1)  # 1h, 2h, 3h... hoặc tùy ý
            # tìm index gần nhất
            idx = min(range(len(times)), key=lambda j: abs(datetime.fromisoformat(times[j]) - target_time))
            t = times[idx]
            points.append({
                "ts": int(datetime.fromisoformat(t).timestamp()*1000),
                "temperature": temps[idx],
                "humidity": hums[idx],
                "weather_desc": WEATHER_CODE_MAP.get(codes[idx], "?")
            })
        return points
    except Exception as e:
        logger.warning(f"Weather 6points error: {e}")
        return []

# ================== AI HELPER ==================
def call_ai_api(temp, humi):
    """Gợi ý chăm sóc + dự báo dựa trên temp/humi"""
    def local_sections(temp, humi):
        nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
        care = []
        if temp >= 35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
        elif temp >= 30: care.append("Tưới đủ nước, theo dõi thường xuyên")
        elif temp <= 15: care.append("Giữ ấm, tránh sương muối")
        else: care.append("Nhiệt độ bình thường")
        if humi <= 40: care.append("Độ ẩm thấp: tăng tưới")
        elif humi <= 60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else: care.append("Độ ẩm ổn định cho rau muống")
        return " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"])

    try:
        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        prompt = f"Nhiệt độ {temp}°C, độ ẩm {humi}% tại An Phú, HCM. Viết dự báo + gợi ý chăm sóc."
        r = requests.post(AI_API_URL, headers=headers, json={"inputs": prompt, "options":{"wait_for_model":True}}, timeout=20)
        r.raise_for_status()
        out = r.json()
        text = ""
        if isinstance(out, list) and out:
            text = out[0].get("generated_text","") if isinstance(out[0],dict) else str(out[0])
        return text.strip() or local_sections(temp,humi)
    except Exception as e:
        logger.warning(f"AI API failed: {e}")
        return local_sections(temp,humi)

# ================== THINGSBOARD ==================
def send_to_thingsboard(points):
    """Push từng point cho TB"""
    try:
        payload = {}
        for p in points:
            for key in ["temperature","humidity","weather_desc","advice"]:
                if key not in payload:
                    payload[key] = []
                payload[key].append({"ts": p["ts"], "value": p.get(key, "")})
        r = requests.post(TB_DEVICE_PUSH_URL, json=payload, timeout=10)
        logger.info(f"TB push response: {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status":"running","device":TB_DEMO_TOKEN[:4]+"***"}

@app.get("/last")
def last_endpoint():
    return _last_payload or {"message":"Chưa có dữ liệu"}

# ================== BACKGROUND LOOP ==================
def background_loop():
    global _last_payload
    while True:
        try:
            points = get_weather_6points()
            for p in points:
                p["advice"] = call_ai_api(p["temperature"], p["humidity"])
                p["location"] = "An Phú, Hồ Chí Minh"
                p["crop"] = "Rau muống"
            send_to_thingsboard(points)
            _last_payload = points
        except Exception as e:
            logger.error(f"Background loop error: {e}")
        time.sleep(LOOP_INTERVAL)

@app.on_event("startup")
def start_background_loop():
    threading.Thread(target=background_loop, daemon=True).start()
