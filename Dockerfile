# サーバーに上げる場合用 (Fly.io / Render / VPS など Docker が使える先)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py edit.py diarize.py ./
EXPOSE 8765
CMD ["python3", "app.py", "--port", "8765"]
