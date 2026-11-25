FROM python:3.11-slim

WORKDIR /app/bridge
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static ./static

# process/stages.yaml монтируется томом в /app/process: правка процесса = перезапуск контейнера, без пересборки.
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
EXPOSE 8787
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
