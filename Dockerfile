FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Python печатает логи сразу, а не пачками — удобнее смотреть в Render
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
