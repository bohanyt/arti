"""Numeric-only enrichment spans on the existing telemetry/journal channel."""
import time


class OfferSpans:
    def __init__(self):
        self.started = time.perf_counter()
        self.spans = {}

    def mark(self, name):
        now = time.perf_counter()
        self.spans[name] = round((now - self.started) * 1000, 3)
        self.started = now

    def emit(self, journal):
        journal(dict(ev="live7_offer_spans", spans=self.spans))
