FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Модель собирается при сборке образа из data/ (воспроизводимо), в git её нет
RUN python make_dataset.py && python train.py | tail -6

# Не root. /data — том со state.db (сессии, статистика); секреты приходят через env_file
RUN useradd -u 1000 -m bot && mkdir /data && chown bot /data
USER bot
WORKDIR /data
ENV PYTHONPATH=/app

CMD ["python", "/app/service.py"]
