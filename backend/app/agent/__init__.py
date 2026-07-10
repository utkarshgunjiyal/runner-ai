"""Runner.ai V2 — autonomous execution layer.

Wraps V1.5 services as tools and (in later phases) plans/validates/executes
them. Phase 1 provides only the ToolSpec model + registry. This package may
import from ``app.services`` but ``app.services`` must never import from here.
"""
