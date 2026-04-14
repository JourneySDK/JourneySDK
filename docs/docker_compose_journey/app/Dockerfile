FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "psycopg[binary]==3.2.10"

COPY server.py /app/server.py

CMD ["python", "-u", "/app/server.py"]
