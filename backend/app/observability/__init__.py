"""Operational observability (Phase 42A): provider-neutral metrics + correlation.

Injectable abstractions with safe, off-by-default posture. Nothing here calls a
paid API or logs sensitive data; metric labels are guarded against
high-cardinality/sensitive keys.
"""
