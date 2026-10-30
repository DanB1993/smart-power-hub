FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir tapo python-kasa cryptography

COPY app.py /app/app.py
COPY static /app/static

EXPOSE 8050

CMD ["python3", "/app/app.py"]
