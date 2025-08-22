import json
import logging
from datetime import datetime
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
app = FastAPI()

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    try:
        body = await req.json()
        logging.info("📩 Got payload:")
        logging.info(json.dumps(body, ensure_ascii=False, indent=2))

        # Tạo advice_text giả lập
        shared = body.get("shared", {})
        advice_text = f"AI advice placeholder for crop {shared.get('crop','unknown')}"

        return {"status": "ok", "advice_text": advice_text}
    except Exception as e:
        logging.error(f"❌ Error handling webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

@app.get("/")
def root():
    return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting FastAPI server on http://127.0.0.1:10000")
    uvicorn.run(app, host="127.0.0.1", port=10000)
