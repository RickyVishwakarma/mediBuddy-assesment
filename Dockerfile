# Optional. The supported way to run this is the two commands in the README; this is
# here for anyone whose local Python is the wrong version or who would rather not create
# a virtualenv.
#
#   docker build -t weather-advisory-bot .
#   docker run --rm -p 8000:8000 --env-file .env weather-advisory-bot
#
# Then open http://localhost:8000.
#
# The API key is passed in at run time via --env-file and is never baked into the image.

FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so editing the app doesn't invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
