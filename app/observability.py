"""Optional OpenTelemetry wiring; disabled when no OTLP endpoint is configured."""

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def set_trace_attributes(**attributes) -> None:
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
    except Exception:
        return


def _configure_provider():
    if not settings.otel_exporter_otlp_endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if isinstance(trace.get_tracer_provider(), TracerProvider):
            return trace.get_tracer_provider()
        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        )
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        logger.info("OpenTelemetry export enabled: %s", endpoint)
        return provider
    except Exception:
        logger.exception("OpenTelemetry provider initialization failed")
        return None


def configure_observability(app, engines: list) -> None:
    if _configure_provider() is None:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        for engine in engines:
            SQLAlchemyInstrumentor().instrument(engine=engine)
        RedisInstrumentor().instrument()
    except Exception:
        logger.exception("API OpenTelemetry instrumentation failed")


def configure_worker_observability(engine) -> None:
    if _configure_provider() is None:
        return
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        CeleryInstrumentor().instrument()
        RedisInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument(engine=engine)
    except Exception:
        logger.exception("worker OpenTelemetry instrumentation failed")
