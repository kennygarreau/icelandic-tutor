import { WebTracerProvider } from '@opentelemetry/sdk-trace-web';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch';
import { DocumentLoadInstrumentation } from '@opentelemetry/instrumentation-document-load';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { Resource } from '@opentelemetry/resources';
import { W3CTraceContextPropagator, CompositePropagator, W3CBaggagePropagator } from '@opentelemetry/core';
import { trace, SpanStatusCode } from '@opentelemetry/api';
import { MeterProvider, PeriodicExportingMetricReader, ExplicitBucketHistogramAggregation, View } from '@opentelemetry/sdk-metrics';
import { OTLPMetricExporter } from '@opentelemetry/exporter-metrics-otlp-http';
import { onLCP, onINP, onCLS, onFCP, onTTFB } from 'web-vitals';

const collectorUrl = process.env.REACT_APP_OTEL_COLLECTOR_URL || '';
const resource = new Resource({ 'service.name': 'icelandic-tutor-frontend' });

// ── Traces ────────────────────────────────────────────────────────────────────
const traceProvider = new WebTracerProvider({ resource });

if (collectorUrl) {
  traceProvider.addSpanProcessor(
    new BatchSpanProcessor(
      new OTLPTraceExporter({ url: `${collectorUrl}/v1/traces` })
    )
  );
} else {
  console.debug('[OTel] REACT_APP_OTEL_COLLECTOR_URL not set — telemetry not exported');
}

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
    views: [
      new View({
        aggregation: new ExplicitBucketHistogramAggregation(
          [0, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.3, 0.5, 0.75, 1.0]
        ),
        instrumentName: 'web_vital_cls',
      }),
    ],
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
  const clsHist  = meter.createHistogram('web_vital_cls',  { description: 'Cumulative Layout Shift' });
  const fcpHist  = meter.createHistogram('web_vital_fcp',  { description: 'First Contentful Paint',     unit: 'ms' });
  const ttfbHist = meter.createHistogram('web_vital_ttfb', { description: 'Time to First Byte',         unit: 'ms' });

  onLCP (m => lcpHist.record (m.value, { rating: m.rating }));
  onINP (m => inpHist.record (m.value, { rating: m.rating }), { reportAllChanges: true });
  onCLS (m => clsHist.record (m.value, { rating: m.rating }), { reportAllChanges: true });
  onFCP (m => fcpHist.record (m.value, { rating: m.rating }));
  onTTFB(m => ttfbHist.record(m.value, { rating: m.rating }));

  // INP and CLS are only finalised when the user leaves — flush before tab closes
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') meterProvider.forceFlush();
  });
}

export const tracer = trace.getTracer('icelandic-tutor-frontend');
export { SpanStatusCode };
