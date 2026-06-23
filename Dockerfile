# Stage 1 : le builder
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Installation de mes deps dans un prefixe
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# Stage 2 
FROM python:3.12-slim AS runtime
# Utilisateur non-root
RUN groupadd -r app && useradd -r -g app -d /app -s /usr/sbin/nologin app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
COPY --from=builder /install /usr/local
COPY app/ ./app/
# Dossier scratch pour les CSV temporaires pendant le traitement 
RUN mkdir -p /tmp/arteci && chown -R app:app /app /tmp/arteci
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# gunicorn avec workers uvicorn : serveur ASGI de production
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", "-b", "0.0.0.0:8000", "--timeout", "300"]