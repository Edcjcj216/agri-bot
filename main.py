import os
import time
import logging
import requests
import threading
from fastapi import FastAPI
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_ID = os.getenv("TB_DEVICE_ID", "")  # Device ID trên ThingsBoard
TB_API_URL = f"https://thingsboard.cloud/api/plugins/telemetry/DEVICE/{TB_DEVICE_ID}/values/timeseries?keys=temperature,humidity,battery"
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

def get_weather_forecast():
    try:
        today_dt = datetime.now()
        tomorrow_dt = today_dt + timedelta(days=1)
        yesterday_dt = today_dt - timedelta(days=1)
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min",
            "hourly": "relativehumidity_2m",
            "timezone": "Asia/Ho_Chi_Minh",
            "start_date": yesterday_dt.strftime("%Y-%m-%d"),
            "end_date": tomorrow_dt.strftime("%Y-%m-%d")
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def mean(lst):
            return round(sum(lst)/len(lst), 1) if lst else 0

        weather_yesterday = {
            "weather_yesterday_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[0], "?"),
            "weather_yesterday_max": daily.get("temperature_2m_max",[0])[0],
            "weather_yesterday_min": daily.get("temperature_2m_min",[0])[0],
            "humidity_yesterday": mean(hourly.get("relativehumidity_2m", [])[:24])
        }
        weather_today = {
            "weather_today_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[1], "?"),
            "weather_today_max": daily.get("temperature_2m_max",[0])[1],
            "weather_today_min": daily.get("temperature_2m_min",[0])[1],
            "humidity_today": mean(hourly.get("relativehumidity_2m", [])[24:48])
        }
        weather_tomorrow = {
            "weather_tomorrow_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[2], "?"),
            "weather_tomorrow_max": daily.get("temperature_2m_max",[0])[2],
            "weather_tomorrow_min": daily.get("temperature_2m_min",[0])[2],
            "humidity_tomorrow": mean(hourly.get("relativehumidity_2m", [])[48:72])
        }
        return {**weather_yesterday, **weather_today, **weather_tomorrow}
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {k: 0 if "max" in k or "min" in k or "humidity" in k else "?" 
                for k in ["weather_yesterday_desc","weather_yesterday_max","weather_yesterday_min","humidity_yesterday",
                          "weather_today_desc","weather_today_max","weather_today_min","humidity_today",
                          "weather_tomorrow_desc","weather_tomorrow_max","weather_tomorrow_min","humidity_tomorrow"]}

# ================== AI HELPER ==================
def call_ai_api(data):
    def local_sections(temp, humi):
        pred = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
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
        return {
            "prediction": pred,
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": " | ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
            "advice": " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"])
        }

    try:
        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        prompt = f"Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}% tại An Phú, HCM. Viết dự báo + gợi ý chăm sóc."
        r = requests.post(AI_API_URL, headers=headers, json={"inputs": prompt, "options":{"wait_for_model":True}}, timeout=20)
        r.raise_for_status()
        out = r.json()
        text = ""
        if isinstance(out, list) and out:
            text = out[0].get("generated_text","") if isinstance(out[0],dict) else str(out[0])
        sec = local_sections(data['temperature'], data['humidity'])
        sec['advice'] = text.strip() or sec['advice']
        return sec
    except Exception as e:
        logger.warning(f"AI API failed: {e}")
        return local_sections(data['temperature'], data['humidity'])

# ================== THINGSBOARD ==================
def get_last_telemetry():
    try:
        r = requests.get(TB_API_URL, timeout=10)
        r.raise_for_status()
        resp = r.json()
        # Format last values
        last = {k: v[-1]['value'] for k,v in resp.items() if v}
        return last
    except Exception as e:
        logger.warning(f"Failed to get last telemetry: {e}")
        return None

def send_to_thingsboard(data):
    try:
        logger.info(f"Sending to TB: {data}")
        r = requests.post(TB_DEVICE_PUSH_URL, json=data, timeout=10)
        logger.info(f"TB response: {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== BACKGROUND LOOP ==================
def background_loop():
    global _last_payload
    while True:
        try:
            last_data = get_last_telemetry()
            if last_data:
                weather = get_weather_forecast()
                ai_result = call_ai_api(last_data)
                merged = {**last_data, **weather, **ai_result, "location":"An Phú, Hồ Chí Minh", "crop":"Rau muống"}
                send_to_thingsboard(merged)
                _last_payload = merged
            else:
                logger.info("No last telemetry available yet.")
        except Exception as e:
            logger.error(f"Background loop error: {e}")
        time.sleep(LOOP_INTERVAL)

@app.on_event("startup")
def start_background_loop():
    threading.Thread(target=background_loop, daemon=True).start()

@app.get("/last")
def last_telemetry_endpoint():
    return _last_payload or {"message":"Chưa có dữ liệu"}
