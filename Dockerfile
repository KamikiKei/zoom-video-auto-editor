# Hugging Face Spaces (Docker) 用
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py edit.py diarize.py ./
RUN mkdir -p /app/jobs && chmod 777 /app/jobs
EXPOSE 7860
CMD ["python3", "app.py", "--port", "7860"]
