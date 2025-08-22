import os
import time
import json
import logging
import requests
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))  # giây

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: Optional[float] = None

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang", 1: "Trời quang nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù đóng băng", 51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa",
    55: "Mưa phùn dày", 61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày", 80: "Mưa rào nhẹ",
    81: "Mưa rào vừa", 82: "Mưa rào mạnh", 95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

weather_cache = {"ts": 0, "data": {}}

def get_weather_forecast():
    now = datetime.now()
    if time.time() - weather_cache["ts"] < 900:  # cache 15 phút
        return weather_cache["data"]

    try:
        start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m,relativehumidity_2m,weathercode,time",
            "timezone": "Asia/Ho_Chi_Minh",
            "start_date": start_date,
            "end_date": end_date
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def mean(lst):
            return round(sum(lst)/len(lst),1) if lst else 0

        # Hôm qua / hôm nay / ngày mai
        weather_yesterday = {
            "weather_yesterday_desc": WEATHER_CODE_MAP.get(daily["weathercode"][0], "?") if "weathercode" in daily and len(daily.get("weathercode", []))>0 else "?",
            "weather_yesterday_max": daily["temperature_2m_max"][0] if "temperature_2m_max" in daily and len(daily.get("temperature_2m_max", []))>0 else 0,
            "weather_yesterday_min": daily["temperature_2m_min"][0] if "temperature_2m_min" in daily and len(daily.get("temperature_2m_min", []))>0 else 0,
            "humidity_yesterday": mean(hourly.get("relativehumidity_2m", [])[:24])
        }
        weather_today = {
            "weather_today_desc": WEATHER_CODE_MAP.get(daily["weathercode"][1], "?") if "weathercode" in daily and len(daily.get("weathercode", []))>1 else "?",
            "weather_today_max": daily["temperature_2m_max"][1] if "temperature_2m_max" in daily and len(daily.get("temperature_2m_max", []))>1 else 0,
            "weather_today_min": daily["temperature_2m_min"][1] if "temperature_2m_min" in daily and len(daily.get("temperature_2m_min", []))>1 else 0,
            "humidity_today": mean(hourly.get("relativehumidity_2m", [])[24:48])
        }
        weather_tomorrow = {
            "weather_tomorrow_desc": WEATHER_CODE_MAP.get(daily["weathercode"][2], "?") if "weathercode" in daily and len(daily.get("weathercode", []))>2 else "?",
            "weather_tomorrow_max": daily["temperature_2m_max"][2] if "temperature_2m_max" in daily and len(daily.get("temperature_2m_max", []))>2 else 0,
            "weather_tomorrow_min": daily["temperature_2m_min"][2] if "temperature_2m_min" in daily and len(daily.get("temperature_2m_min", []))>2 else 0,
            "humidity_tomorrow": mean(hourly.get("relativehumidity_2m", [])[48:72])
        }

        # Lấy dữ liệu hourly
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])

        dt_list = []
        for t in times:
            try:
                dt = datetime.fromisoformat(t)
            except Exception:
                dt = None
            dt_list.append(dt)

        # tìm index giờ tiếp theo so với now
        start_idx = 0
        for i, dt in enumerate(dt_list):
            if dt and dt > now:
                start_idx = i
                break

        result = {**weather_yesterday, **weather_today, **weather_tomorrow}
        hourly_forecast_list = []
        for n in range(1, 7):
            idx = start_idx + (n - 1)
            if idx < len(dt_list) and dt_list[idx] is not None:
                ts_str = dt_list[idx].strftime("%H:%M %d-%m")
                temp_val = round(temps[idx], 1) if idx < len(temps) else 0
                hum_val = round(hums[idx], 1) if idx < len(hums) else 0
                desc_val = WEATHER_CODE_MAP.get(codes[idx], "?") if idx < len(codes) else "?"
            else:
                ts_str = "-"
                temp_val = 0
                hum_val = 0
                desc_val = "?"

            friendly = f"{n} giờ tiếp theo: {ts_str} — {temp_val}°C — {desc_val}"
            result[f"{n}_gio_tiep_theo"] = friendly

            hourly_forecast_list.append({
                "hours_ahead": n,
                "time": ts_str,
                "temperature": temp_val,
                "humidity": hum_val,
                "desc": desc_val
            })

        result["hourly_forecast_next_6h"] = hourly_forecast_list

        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result

    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        fallback = {"weather_yesterday_desc":"?","weather_yesterday_max":0,"weather_yesterday_min":0,"humidity_yesterday":0,
                    "weather_today_desc":"?","weather_today_max":0,"weather_today_min":0,"humidity_today":0,
                    "weather_tomorrow_desc":"?","weather_tomorrow_max":0,"weather_tomorrow_min":0,"humidity_tomorrow":0}
        for i in range(1,7):
            fallback[f"{i}_gio_tiep_theo"] = f"{i} giờ tiếp theo: - — 0°C — ?"
        fallback["hourly_forecast_next_6h"] = []
        return fallback

# ================== AI HELPER ==================
def get_advice(temp, humi):
    nutrition = ["Ưu tiên Kali (K)","Cân bằng NPK","Bón phân hữu cơ"]
    care = []
    if temp >=35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
    elif temp >=30: care.append("Tưới đủ nước, theo dõi thường xuyên")
    elif temp <=15: care.append("Giữ ấm, tránh sương muối")
    else: care.append("Nhiệt độ bình thường")
    if humi <=40: care.append("Độ ẩm thấp: tăng tưới")
    elif humi <=60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif humi >=85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else: care.append("Độ ẩm ổn định cho rau muống")
    return {
        "advice": " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"]),
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": " | ".join(care),
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    }

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict) -> bool:
    try:
        logger.info("TB ▶ %s", json.dumps(data, ensure_ascii=False))
        headers = {"Content-Type": "application/json"}
        r = requests.post(TB_DEVICE_URL, json=data, headers=headers, timeout=10)
        logger.info("TB ◀ status=%s body=%s", r.status_code, (r.text[:1000] if r.text else ""))
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        resp_text = ""
        try:
            resp_text = e.response.text if e.response is not None else ""
        except Exception:
            resp_text = ""
        logger.error("ThingsBoard push error: %s - resp: %s", e, resp_text)
        if hasattr(e, "response") and e.response is not None:
            if e.response.status_code in (401, 403):
                logger.error("HTTP %s from ThingsBoard — kiểm tra TB_DEMO_TOKEN / Device Credentials", e.response.status_code)
        return False

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status":"running","demo_token":TB_DEMO_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info("ESP32 ▶ %s", data.dict())
    advice_data = get_advice(data.temperature,data.humidity)
    weather_data = get_weather_forecast()
    merged = {
        **data.dict(),
        **advice_data,
        **weather_data,
        "location":"An Phú, Hồ Chí Minh",
        "crop":"Rau muống"
    }
    ok = send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged if ok else None, "ok": ok}

@app.get("/tb-test")
def tb_test():
    sample = {"temperature": 25.5, "humidity": 60.0, "note": "tb-test"}
    weather_data = get_weather_forecast()
    advice_data = get_advice(sample["temperature"], sample["humidity"])
    merged = {**sample, **advice_data, **weather_data, "location":"Test"}
    ok = send_to_thingsboard(merged)
    return {"ok": ok, "payload": merged}

# ================== AUTO LOOP ==================
async def auto_loop():
    while True:
        try:
            sample = {"temperature":30.1,"humidity":69.2}
            advice_data = get_advice(sample["temperature"],sample["humidity"])
            weather_data = get_weather_forecast()
            merged = {
                **sample,
                **advice_data,
                **weather_data,
                "location":"An Phú, Hồ Chí Minh",
                "crop":"Rau muống"
            }
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def start_auto_loop():
    asyncio.create_task(auto_loop())

if __name__ == "__main__":
    # cho test local: python main.py
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
