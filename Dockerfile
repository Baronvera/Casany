# ---- Build stage ----
FROM python:3.11-slim AS builder
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps -w /wheels -r requirements.txt

# ---- Runtime stage ----
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TZ=America/Bogota
# usuario no-root
RUN useradd -m appuser
COPY --from=builder /wheels /wheels
RUN pip install --no-cache /wheels/*
# copia tu app
COPY main.py .
COPY prompt_cassany_gpt_final.txt .
# si tienes más módulos/archivos:
COPY crud.py database.py models.py routes_agent.py hubspot_utils.py utils_intencion.py utils_mensaje_whatsapp.py woocommerce_gpt_utils.py ./
# arranque uvicorn
EXPOSE 8080
USER appuser
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
