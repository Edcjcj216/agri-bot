from fastapi import FastAPI, Request
import requests

app = FastAPI()

# Config
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"

@app.post("/esp32-data")
async def receive_data(request: Request):
    data = await request.json()
    temperature = data.get("temperature")
    humidity    = data.get("humidity")

    # Fake AI prediction & advice (có thể thay bằng API khác)
    prediction = f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"
    advice     = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"

    telemetry = {
        "prediction": prediction,
        "advice": advice
    }

    try:
        # Push lên ThingsBoard
        r = requests.post(
            THINGSBOARD_URL,
            json=telemetry,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        tb_status = r.status_code
    except Exception as e:
        tb_status = str(e)

    return {
        "status": "ok",
        "received": data,
        "prediction": prediction,
        "advice": advice,
        "thingsboard_status": tb_status
    }
