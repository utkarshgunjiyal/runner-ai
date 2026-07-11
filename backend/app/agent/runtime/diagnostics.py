"""GitHub runtime selection diagnostics (Phase 46.2.3). DIAGNOSTIC ONLY.

Emits safe, structured events that trace how one request flows from the chosen
execution path → capability candidates → selected capability → tool binding → MCP
invocation. It changes NO behavior: every emitter only writes a structured log line
(``get_logger("agent.diagnostics")``; the JSON formatter auto-attaches the
per-request ``request_id``) and mirrors the same safe record onto
``run_context.metadata["diagnostics"]`` for tests and the diagnostic helper. A
diagnostic failure never affects the run (all emits are best-effort).

Redaction is by construction — only these safe fields are ever included:
capability/tool ids, tool kind, provider, MCP ``server_id``/``tool_name``,
``handler_ref``, deterministic scores, matched fields/terms, argument KEY names
(never values), item counts, durations, error codes. It never logs the token, auth
headers, tool arguments' values, raw MCP payloads, request text, prior conversation
text, or issue/PR bodies. Request text is represented by a stable short hash +
length + intent labels only.
"""

from __future__ import annotations

import hashlib

from app.logging_config import get_logger

_logger = get_logger("agent.diagnostics")

# The top-N candidates recorded (bounded — never the whole catalog).
_MAX_CANDIDATES = 8


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def hash12(text: str | None) -> str:
    """A stable short hash of text — used instead of logging raw request text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def request_fingerprint(run_context) -> dict:
    """A privacy-safe fingerprint of the current request — never the raw text."""
    text = getattr(run_context, "user_request", "") or ""
    interpretation = (getattr(run_context, "metadata", {}) or {}).get("interpretation") or {}
    return {
        "request_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        "request_length": len(text),
        "intent_labels": list(interpretation.get("intents") or []),
    }


def binding_view(tool) -> dict:
    """Parse a ToolSpec's safe routing binding. For an MCP tool the ``handler_ref``
    is ``mcp:<server_id>:<tool_name>`` — decoded here without touching the registry
    or adapter (proves the id→server/tool mapping)."""
    handler_ref = getattr(tool, "handler_ref", None)
    kind = getattr(getattr(tool, "kind", None), "value", None)
    server_id = tool_name = None
    if isinstance(handler_ref, str) and handler_ref.startswith("mcp:"):
        parts = handler_ref.split(":", 2)
        if len(parts) == 3:
            server_id, tool_name = parts[1], parts[2]
    provider = None
    for tag in getattr(tool, "tags", []) or []:
        if tag in ("github", "gmail", "calendar"):
            provider = tag
            break
    return {
        "capability_id": getattr(tool, "id", None),
        "handler_ref": handler_ref,
        "adapter_kind": kind,
        "provider": provider,
        "server_id": server_id,
        "mcp_tool_name": tool_name,
    }


def candidate_view(match, rank: int) -> dict:
    """Safe view of one ranked candidate (no schemas, no payloads)."""
    tool = match.tool
    b = binding_view(tool)
    score = float(getattr(match, "score", 0.0) or 0.0)
    return {
        "rank": rank,
        "capability_id": b["capability_id"],
        "tool_kind": b["adapter_kind"],
        "provider": b["provider"],
        "server_id": b["server_id"],
        "mcp_tool_name": b["mcp_tool_name"],
        # Null embedding/reranker → the hybrid/final score is the keyword score.
        "keyword_score": round(score, 4),
        "final_score": round(score, 4),
        "matched_fields": list(getattr(match, "matched_fields", []) or []),
        "matched_terms": list(getattr(match, "matched_terms", []) or []),
        "enabled": bool(getattr(tool, "enabled", True)),
        "eligible": True,  # ineligible candidates are dropped before this point
    }


def _item_count(output) -> int | None:
    """Count normalized items without exposing content: the first list value in a
    normalized output dict (e.g. repositories/issues/pull_requests)."""
    if not isinstance(output, dict):
        return None
    for key, value in output.items():
        if key == "content":
            continue
        if isinstance(value, list):
            return len(value)
    return None


def _connector_status(run_context, provider) -> str | None:
    if not provider:
        return None
    for snap in (getattr(run_context, "metadata", {}) or {}).get("connectors") or []:
        if isinstance(snap, dict) and snap.get("provider") == provider:
            return snap.get("status")
    return None


def emit(run_context, event: str, **fields) -> dict:
    """Emit one safe diagnostic event (log + metadata mirror). Best-effort."""
    record = {"event": event}
    rid = getattr(run_context, "run_id", None)
    if rid is not None:
        record["run_id"] = rid
    tid = getattr(run_context, "thread_id", None)
    if tid is not None:
        record["thread_id"] = tid
    record.update(fields)
    try:
        _logger.info(event, extra=record)
    except Exception:  # noqa: BLE001 - diagnostics must never break a run
        pass
    try:
        meta = getattr(run_context, "metadata", None)
        if isinstance(meta, dict):
            meta.setdefault("diagnostics", []).append(record)
    except Exception:  # noqa: BLE001
        pass
    return record


# --------------------------------------------------------------------------- #
# High-level emitters (used by the runtime)
# --------------------------------------------------------------------------- #

def runtime_path_selected(run_context, *, path: str, reason: str | None = None) -> None:
    emit(run_context, "agent.runtime_path_selected", path=path, behavior_reason=reason,
         **request_fingerprint(run_context))


def capability_candidates(run_context, matches, *, path: str) -> None:
    views = [candidate_view(m, i) for i, m in enumerate(matches[:_MAX_CANDIDATES])]
    emit(run_context, "agent.capability_candidates", path=path,
         candidate_count=len(matches), candidates=views)


def capability_selected(run_context, tool, *, path: str, rank: int, score: float) -> None:
    b = binding_view(tool)
    emit(run_context, "agent.capability_selected", path=path, final_rank=rank,
         final_score=round(float(score or 0.0), 4), **b)


def tool_binding_resolved(run_context, tool) -> None:
    b = binding_view(tool)
    emit(run_context, "agent.tool_binding_resolved", binding_lookup_success=bool(b["server_id"]), **b)


def mcp_tool_invoked(run_context, tool, args, *, timeout=None, retry_attempt: int = 1) -> None:
    b = binding_view(tool)
    emit(run_context, "agent.mcp_tool_invoked",
         capability_id=b["capability_id"], server_id=b["server_id"], tool_name=b["mcp_tool_name"],
         connector_status=_connector_status(run_context, b["provider"]),
         timeout_seconds=timeout, retry_attempt=retry_attempt,
         argument_keys=sorted(str(k) for k in (args or {}).keys()))


def mcp_tool_completed(run_context, tool, result, *, attempts: int) -> None:
    b = binding_view(tool)
    success = bool(getattr(result, "success", False))
    output = getattr(result, "output", None)
    emit(run_context, "agent.mcp_tool_completed",
         capability_id=b["capability_id"], server_id=b["server_id"], tool_name=b["mcp_tool_name"],
         success=success,
         duration_ms=(getattr(result, "metadata", {}) or {}).get("duration_ms"),
         item_count=_item_count(output) if success else None,
         error_code=None if success else getattr(result, "error_code", None),
         retry_count=max(0, attempts - 1))
