import { WebTracerProvider } from '@opentelemetry/sdk-trace-web';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch';
import { DocumentLoadInstrumentation } from '@opentelemetry/instrumentation-document-load';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { W3CTraceContextPropagator, CompositePropagator, W3CBaggagePropagator } from '@opentelemetry/core';
import { trace, SpanStatusCode } from '@opentelemetry/api';
import { MeterProvider, PeriodicExportingMetricReader } from '@opentelemetry/sdk-metrics';
import { OTLPMetricExporter } from '@opentelemetry/exporter-metrics-otlp-http';
import { onLCP, onINP, onCLS, onFCP, onTTFB } from 'web-vitals';

const collectorUrl = import.meta.env.VITE_OTEL_COLLECTOR_URL || '';
const resource = resourceFromAttributes({ 'service.name': 'icelandic-tutor-frontend' });

// ── Traces ────────────────────────────────────────────────────────────────────
if (!collectorUrl) {
  console.debug('[OTel] VITE_OTEL_COLLECTOR_URL not set — telemetry not exported');
}

const traceProvider = new WebTracerProvider({
  resource,
  spanProcessors: collectorUrl
    ? [new BatchSpanProcessor(new OTLPTraceExporter({ url: `${collectorUrl}/v1/traces` }))]
    : [],
});

traceProvider.register({
  propagator: new CompositePropagator({
    propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()],
  }),
});

registerInstrumentations({
  instrumentations: [
    new DocumentLoadInstrumentation(),
    new FetchInstrumentation({
      propagateTraceHeaderCorsUrls: [/.*/],
      clearTimingResources: true,
    }),
  ],
  tracerProvider: traceProvider,
});

// ── Metrics — Core Web Vitals ─────────────────────────────────────────────────
if (collectorUrl) {
  const meterProvider = new MeterProvider({
    resource,
    readers: [
      new PeriodicExportingMetricReader({
        exporter: new OTLPMetricExporter({ url: `${collectorUrl}/v1/metrics` }),
        exportIntervalMillis: 30000,
      }),
    ],
  });

  const meter = meterProvider.getMeter('icelandic-tutor-frontend');

  const lcpHist  = meter.createHistogram('web_vital_lcp',  { description: 'Largest Contentful Paint',  unit: 'ms' });
  const inpHist  = meter.createHistogram('web_vital_inp',  { description: 'Interaction to Next Paint',  unit: 'ms' });
  const clsHist  = meter.createHistogram('web_vital_cls',  { description: 'Cumulative Layout Shift',
    advice: { explicitBucketBoundaries: [0, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 1.0] } });
  const fcpHist  = meter.createHistogram('web_vital_fcp',  { description: 'First Contentful Paint',     unit: 'ms' });
  const ttfbHist = meter.createHistogram('web_vital_ttfb', { description: 'Time to First Byte',         unit: 'ms' });

  onLCP (m => lcpHist.record (m.value, { rating: m.rating }), { reportAllChanges: true });
  onINP (m => inpHist.record (m.value, { rating: m.rating }), { reportAllChanges: true });
  onCLS (m => clsHist.record (m.value, { rating: m.rating }));
  onFCP (m => fcpHist.record (m.value, { rating: m.rating }));
  onTTFB(m => ttfbHist.record(m.value, { rating: m.rating }));

  // Flush after a short delay on hide/unload so LCP finalisation (which fires
  // on the same events) has time to record before the export goes out.
  const flushDelayed = () => setTimeout(() => meterProvider.forceFlush(), 100);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushDelayed();
  });
  addEventListener('pagehide', flushDelayed);
}

export const tracer = trace.getTracer('icelandic-tutor-frontend');
export { SpanStatusCode };
