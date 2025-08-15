from fastapi import FastAPI, Query, Body
import requests
import os
import google.generativeai as genai
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI()

# Lấy API key từ biến môi trường Render
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("Thiếu GEMINI_API_KEY. Hãy đặt key trong Render → Environment.")

if not WEATHER_API_KEY:
    raise ValueError("Thiếu WEATHER_API_KEY. Hãy đặt key trong Render → Environment.")

# Khởi tạo Gemini
genai.configure(api_key=GEMINI_API_KEY)

@app.get("/")
def home():
    return JSONResponse(
        content={"message": "AI Bot Nông nghiệp đang chạy!"},
        media_type="application/json; charset=utf-8"
    )

@app.head("/")
async def healthcheck():
    return PlainTextResponse("OK", media_type="text/plain; charset=utf-8")

@app.get("/advise")
def advise(crop: str = Query(...), location: str = Query(...)):
    return get_advice(crop, location)

@app.post("/predict")
def predict(payload: dict = Body(...)):
    crop = payload.get("crop")
    location = payload.get("location")
    if not crop or not location:
        return JSONResponse(content={"error": "Thiếu crop hoặc location"}, status_code=400)
    return get_advice(crop, location)

def get_advice(crop, location):
    try:
        # Lấy dữ liệu thời tiết
        weather_url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?q={location}&appid={WEATHER_API_KEY}&units=metric&lang=vi"
        )
        weather_data = requests.get(weather_url, timeout=10).json()
        print("DEBUG Weather API:", weather_data)

        if "list" not in weather_data:
            return JSONResponse(
                content={"error": f"Không lấy được dữ liệu thời tiết: {weather_data}"},
                status_code=500
            )

        forecast = weather_data["list"][0]
        temp = forecast["main"]["temp"]
        desc = forecast["weather"][0]["description"]

        # Prompt cho AI
        prompt = (
            f"Tôi là chuyên gia nông nghiệp. Với cây {crop} ở {location}, "
            f"nhiệt độ {temp}°C và thời tiết {desc}, "
            "hãy đưa ra gợi ý dinh dưỡng và chăm sóc phù hợp trong tuần tới."
        )
        print("DEBUG Prompt:", prompt)

        # Gọi Gemini API (đúng cú pháp)
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        print("DEBUG AI Response:", response)

        advice = getattr(response, "text", None) or str(response)

        return JSONResponse(
            content={
                "crop": crop,
                "location": location,
                "temperature": temp,
                "weather": desc,
                "advice": advice
            },
            media_type="application/json; charset=utf-8"
        )

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print("DEBUG ERROR:", error_details)
        return JSONResponse(
            content={"error": str(e), "trace": error_details},
            status_code=500
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
