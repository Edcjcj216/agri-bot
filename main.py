from fastapi import FastAPI, Query, HTTPException
import httpx  # Thay thế requests bằng httpx async
import os
import logging
from openai import OpenAI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# Cấu hình logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AgriBot API",
    description="API tư vấn nông nghiệp thông minh",
    version="2.0",
    docs_url="/api-docs"
)

# Cấu hình CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Model request
class AdviceRequest(BaseModel):
    crop: str
    location: str
    lang: Optional[str] = "vi"

# Lấy API key từ biến môi trường
try:
    OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
    WEATHER_API_KEY = os.environ["WEATHER_API_KEY"]
except KeyError as e:
    logger.error(f"Missing environment variable: {str(e)}")
    raise

client = OpenAI(api_key=OPENAI_API_KEY)

# Timeout cấu hình
TIMEOUT_CONFIG = httpx.Timeout(15.0, connect=5.0)
WEATHER_API_URL = "https://api.openweathermap.org/data/2.5/forecast"

@app.get("/")
async def home():
    return {"status": "running", "version": app.version}

@app.head("/")
async def healthcheck():
    return PlainTextResponse("OK")

@app.get("/advise", response_model=dict)
async def get_advise(
    crop: str = Query(..., min_length=2, max_length=50),
    location: str = Query(..., min_length=2, max_length=100)
):
    """Endpoint chính cung cấp tư vấn nông nghiệp"""
    try:
        # 1. Lấy dữ liệu thời tiết với timeout
        async with httpx.AsyncClient(timeout=TIMEOUT_CONFIG) as client_http:
            weather_response = await client_http.get(
                WEATHER_API_URL,
                params={
                    "q": location,
                    "appid": WEATHER_API_KEY,
                    "units": "metric",
                    "lang": "vi"
                }
            )
            weather_response.raise_for_status()
            weather_data = weather_response.json()

            if not weather_data.get("list"):
                raise HTTPException(
                    status_code=404,
                    detail="Không tìm thấy dữ liệu thời tiết cho địa điểm này"
                )

            forecast = weather_data["list"][0]
            weather_info = {
                "temp": forecast["main"]["temp"],
                "humidity": forecast["main"]["humidity"],
                "description": forecast["weather"][0]["description"],
                "icon": forecast["weather"][0]["icon"]
            }

        # 2. Gọi OpenAI API
        prompt = f"""
        Bạn là chuyên gia nông nghiệp. Hãy cung cấp lời khuyên cụ thể về:
        - Cây trồng: {crop}
        - Địa điểm: {location}
        - Điều kiện thời tiết: {weather_info['description']}, nhiệt độ {weather_info['temp']}°C
        Yêu cầu:
        1. Ngắn gọn (dưới 150 từ)
        2. Chia thành các mục rõ ràng
        3. Bao gồm cả cảnh báo rủi ro nếu có
        """

        completion = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Bạn là chuyên gia nông nghiệp với 20 năm kinh nghiệm."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=300
        )

        advice = completion.choices[0].message.content

        return JSONResponse({
            "metadata": {
                "model": "gpt-3.5-turbo",
                "weather_source": "OpenWeatherMap"
            },
            "data": {
                "crop": crop,
                "location": location,
                "weather": weather_info,
                "advice": advice
            }
        })

    except httpx.HTTPStatusError as e:
        logger.error(f"Weather API error: {str(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"Lỗi khi lấy dữ liệu thời tiết: {e.response.status_code}"
        )
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Đã xảy ra lỗi hệ thống"
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        timeout_keep_alive=30,
        log_config="log.ini"
    )