# ---- Build stage ----
FROM python:3.11-slim AS builder
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Herramientas de compilación (solo en build stage)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# Construye ruedas de todas las deps
RUN pip wheel --no-cache-dir --no-deps -w /wheels -r requirements.txt

# ---- Runtime stage ----
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    TZ=America/Bogota
# Usuario no root
RUN useradd -m appuser

# Instala deps desde las ruedas generadas (sin tocar internet)
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt

# Copia todo tu proyecto (controla con .dockerignore)
COPY . .

EXPOSE 8080
USER appuser
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

