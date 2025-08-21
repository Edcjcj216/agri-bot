import logging
import random
from datetime import datetime
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import uvicorn

# --- Cấu hình Log ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("agri-bot")

# --- ThingsBoard ---
TB_TOKEN = "pk94asonfacs6mbeuutg"  # Token cố định
TB_URL = f"http://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

app = FastAPI()

# --- Hàm gửi dữ liệu ---
def send_to_thingsboard(payload: dict):
    try:
        logger.info(f"[TB ▶] Payload: {payload}")
        resp = requests.post(TB_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            logger.info(f"[TB ◀] OK {resp.status_code} - {resp.text}")
        else:
            logger.warning(f"[TB ◀] LỖI {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"[TB] EXCEPTION: {e}")

# --- Tạo dữ liệu mẫu ---
def generate_sample_data():
    temperature = round(random.uniform(25, 35), 1)
    humidity = round(random.uniform(60, 80), 1)
    battery = random.randint(50, 100)
    return {
        "time_sent": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "Server-Sim",
        "plant_type": "Rau muống",
        "location_name": "10.80609,106.75222",
        "weather_now_desc": "Nhiều mây",
        "weather_now_temp": temperature,
        "weather_now_humidity": humidity,
        "temperature": temperature,
        "humidity": humidity,
        "battery": battery,
        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
        "advice_care": "Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn định",
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": " | ".join([
            "Ưu tiên Kali (K)",
            "Cân bằng NPK",
            "Bón phân hữu cơ",
            "Tưới đủ nước",
            "Độ ẩm ổn định",
            "Quan sát thực tế"
        ]),
        "prediction": f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"
    }

# --- Job gửi mẫu định kỳ ---
def job_send_sample():
    logger.info("[JOB] Gửi dữ liệu mẫu định kỳ...")
    send_to_thingsboard(generate_sample_data())

scheduler = BackgroundScheduler()
scheduler.add_job(job_send_sample, 'interval', minutes=5)
scheduler.start()

# --- API nhận từ ESP32 ---
@app.post("/telemetry")
async def receive_telemetry(req: Request):
    data = await req.json()
    data["time_sent"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["source"] = "ESP32"
    logger.info(f"[ESP32 ▶] {data}")
    send_to_thingsboard(data)
    return {"status": "OK"}

@app.get("/")
async def root():
    return {"message": "AgriBot server OK. Telemetry gửi mỗi 5 phút."}

# --- Khởi động server ---
if __name__ == "__main__":
    logger.info("[INIT] Gửi dữ liệu mẫu ngay khi start...")
    send_to_thingsboard(generate_sample_data())  # Đảm bảo có dữ liệu ngay lập tức
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)
