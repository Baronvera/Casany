# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Herramientas de compilación necesarias solo en esta etapa
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Construye ruedas para todas las dependencias
RUN pip wheel --no-cache-dir --no-deps -w /wheels -r requirements.txt


# ---- Runtime stage ----
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    TZ=America/Bogota

# Instalar dependencias mínimas necesarias para runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 tzdata \
    && rm -rf /var/lib/apt/lists/*

# Crear usuario no root
RUN useradd -m appuser

# Copiar ruedas y requirements desde la build stage
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --find-links=/wheels -r requirements.txt

# Copiar el proyecto
COPY . .

EXPOSE 8080
USER appuser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
