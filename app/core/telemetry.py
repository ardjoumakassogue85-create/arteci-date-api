"""Traces OpenTelemetry + logs JSON structurés, câblés pour SigNoz (OTLP).

Ce qu'on obtient dans SigNoz :
* **Des traces** sur tout le cycle de vie de la requête. L'auto-instrumentation
  FastAPI crée le span serveur ; les services ajoutent des spans enfants manuels
  (lecture de l'en-tête, download, normalisation, upload). Les attributs portent
  bucket/fichier/colonnes/nombre de lignes/durées.
* **Des logs structurés** (JSON, un objet par ligne) avec ``trace_id`` et
  ``span_id``, pour qu'une ligne de log renvoie directement à sa trace. Les logs
  partent sur stdout (récupérés par le runtime du conteneur) et, quand c'est activé,
  sont aussi poussés vers SigNoz via l'exporteur OTLP **logs**.

Tout se dégrade proprement : si ``OTEL_ENABLED=false`` ou si l'import d'un exporteur
n'est pas dispo, l'API tourne quand même et continue de logger du JSON structuré en
local.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import Settings

_RESERVED = set(logging.makeLogRecord({}).__dict__.keys()) | {"message", "asctime"}


class JsonLogFormatter(logging.Formatter):
    """Rend les enregistrements de log en JSON sur une ligne, corrélés à la trace."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
        }

        # Corrèle avec le span actif s'il y en a un.
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx is not None and ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")

        # Fusionne les extras structurés passés via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """Envoie des logs JSON structurés sur stdout, au niveau configuré."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter(settings.otel_service_name))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Aligne les loggers uvicorn/gunicorn sur notre format JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def _resource(settings: Settings) -> Resource:
    attrs: dict[str, object] = {"service.name": settings.otel_service_name}
    for pair in settings.otel_resource_attributes.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            attrs[k.strip()] = v.strip()
    return Resource.create(attrs)


def _make_span_exporter(settings: Settings):
    """Renvoie un exporteur de spans OTLP pour le protocole configuré, ou None."""
    endpoint = settings.otel_exporter_otlp_endpoint
    insecure = settings.otel_exporter_otlp_insecure
    try:
        if settings.otel_exporter_otlp_protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            return OTLPSpanExporter(endpoint=endpoint)
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
    except Exception as exc:  # pragma: no cover - garde import/version
        logging.getLogger(__name__).warning(
            "OTLP span exporter unavailable; traces will not be exported.",
            extra={"error": str(exc)},
        )
        return None


def setup_tracing(settings: Settings) -> None:
    """Installe un TracerProvider qui exporte les spans vers SigNoz en OTLP."""
    provider = TracerProvider(resource=_resource(settings))
    exporter = _make_span_exporter(settings)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def setup_logs_export(settings: Settings) -> None:
    """Export OTLP des *logs* en best-effort, pour voir dans SigNoz les logs liés aux traces."""
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        provider = LoggerProvider(resource=_resource(settings))
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=settings.otel_exporter_otlp_endpoint,
                    insecure=settings.otel_exporter_otlp_insecure,
                )
            )
        )
        set_logger_provider(provider)
        otlp_handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        logging.getLogger().addHandler(otlp_handler)
    except Exception as exc:  # pragma: no cover - l'export des logs est optionnel
        logging.getLogger(__name__).info(
            "OTLP log export not enabled.", extra={"reason": str(exc)}
        )


def instrument_fastapi(app) -> None:
    """Auto-instrumente l'app FastAPI (crée le span serveur à chaque requête)."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "FastAPI auto-instrumentation failed.", extra={"error": str(exc)}
        )


def setup_telemetry(app, settings: Settings) -> None:
    """Mise en place en un appel : logs JSON toujours ; traces/export de logs si activés."""
    configure_logging(settings)
    if settings.otel_enabled:
        setup_tracing(settings)
        setup_logs_export(settings)
        instrument_fastapi(app)
    logging.getLogger(__name__).info(
        "Telemetry initialized.",
        extra={
            "otel_enabled": settings.otel_enabled,
            "otlp_endpoint": settings.otel_exporter_otlp_endpoint,
            "service": settings.otel_service_name,
        },
    )
