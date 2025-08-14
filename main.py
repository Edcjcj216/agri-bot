from fastapi import FastAPI, Query
import requests
import os
<<<<<<< HEAD
import google.generativeai as genai  # Dùng Gemini API
from fastapi.responses import PlainTextResponse

app = FastAPI()

# Lấy API key từ biến môi trường Render
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("Thiếu GEMINI_API_KEY. Hãy đặt key trong Render → Environment.")

if not WEATHER_API_KEY:
    raise ValueError("Thiếu WEATHER_API_KEY. Hãy đặt key trong Render → Environment.")

# Khởi tạo client Gemini
genai.configure(api_key=GEMINI_API_KEY)

@app.get("/")
def home():
    return {"message": "AI Bot Nông nghiệp đang chạy!"}

# Healthcheck cho UptimeRobot
@app.head("/")
async def healthcheck():
    return PlainTextResponse("OK")

@app.get("/advise")
def advise(crop: str = Query(...), location: str = Query(...)):
    # 1. Lấy dữ liệu thời tiết
=======
from openai import OpenAI
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("Thi?u OPENAI_API_KEY. H�y d?t key trong Render ? Environment.")

if not WEATHER_API_KEY:
    raise ValueError("Thi?u WEATHER_API_KEY. H�y d?t key trong Render ? Environment.")

client = OpenAI(api_key=OPENAI_API_KEY)

@app.get("/")
def home():
    return JSONResponse(
        content={"message": "AI Bot N�ng nghi?p dang ch?y!"},
        media_type="application/json; charset=utf-8"
    )

@app.head("/")
async def healthcheck():
    return PlainTextResponse("OK", media_type="text/plain; charset=utf-8")

@app.get("/advise")
def advise(crop: str = Query(...), location: str = Query(...)):
>>>>>>> b1a396191122e41dffa652c18b53141eb471460a
    weather_url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?q={location}&appid={WEATHER_API_KEY}&units=metric&lang=vi"
    )
    weather_data = requests.get(weather_url).json()
<<<<<<< HEAD

    if "list" not in weather_data:
        return {"error": "Không lấy được dữ liệu thời tiết."}

=======
    if "list" not in weather_data:
        return JSONResponse(
            content={"error": "Kh�ng l?y du?c d? li?u th?i ti?t."},
            media_type="application/json; charset=utf-8"
        )
>>>>>>> b1a396191122e41dffa652c18b53141eb471460a
    forecast = weather_data["list"][0]
    temp = forecast["main"]["temp"]
    desc = forecast["weather"][0]["description"]

<<<<<<< HEAD
    # 2. Gọi AI để phân tích với Gemini
    prompt = (
        f"Tôi là chuyên gia nông nghiệp. Với cây {crop} ở {location}, "
        f"nhiệt độ {temp}°C và thời tiết {desc}, "
        "hãy đưa ra gợi ý dinh dưỡng và chăm sóc phù hợp trong tuần tới."
    )

    completion = genai.chat.create(
        model="gemini-1.5-t",
        messages=[
            {"role": "system", "content": "Bạn là chuyên gia nông nghiệp."},
            {"role": "user", "content": prompt},
        ],
    )

    advice = completion.last["content"][0]["text"]

    return {
        "crop": crop,
        "location": location,
        "temperature": temp,
        "weather": desc,
        "advice": advice,
    }

# Cho phép chạy cục bộ bằng: python main.py
=======
    prompt = (
        f"T�i l� chuy�n gia n�ng nghi?p. V?i c�y {crop} ? {location}, "
        f"nhi?t d? {temp}�C v� th?i ti?t {desc}, "
        "h�y dua ra g?i � dinh du?ng v� cham s�c ph� h?p trong tu?n t?i."
    )

    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "B?n l� chuy�n gia n�ng nghi?p."},
            {"role": "user", "content": prompt},
        ],
    )
    advice = completion.choices[0].message.content

    return JSONResponse(
        content={
            "crop": crop,
            "location": location,
            "temperature": temp,
            "weather": desc,
            "advice": advice,
        },
        media_type="application/json; charset=utf-8"
    )

>>>>>>> b1a396191122e41dffa652c18b53141eb471460a
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
