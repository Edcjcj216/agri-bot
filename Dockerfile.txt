FROM python:3.11-slim

# tạo user non-root
RUN groupadd -r appgroup && useradd -r -g appgroup appuser

WORKDIR /app   # Docker sẽ tạo /app nếu chưa có

# tạo thư mục logs và dữ liệu, set quyền cho appuser
RUN mkdir -p /app/logs /app/data \
    && chown -R appuser:appgroup /app

# copy requirements và cài
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install -r /app/requirements.txt

# copy source
COPY . /app

# đảm bảo quyền cho file sau khi copy
RUN chown -R appuser:appgroup /app

# chạy dưới quyền non-root
USER appuser

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"]
