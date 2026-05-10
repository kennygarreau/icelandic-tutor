"""
OpenTelemetry tracing setup.
Traces are pushed via OTLP HTTP to the collector.
Metrics are exposed on /metrics for Prometheus scrape.
"""
import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)

def setup_tracing(service_name: str) -> trace.Tracer:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c220-fedora:4318")
    resource  = Resource.create({"service.name": service_name})
    provider  = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    logger.info(f"Tracing → {endpoint}  service={service_name}")
    return trace.get_tracer(service_name)
