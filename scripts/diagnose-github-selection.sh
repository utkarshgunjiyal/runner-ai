#!/usr/bin/env bash
# Read-only helper to trace GitHub capability selection from the backend's
# structured JSON logs (Phase 46.2.3). It filters the safe `agent.diagnostics`
# events for one request/run and prints only: runtime path, candidate ranking,
# selected capability, planned tool, resolved binding, invoked MCP tool, and
# completion status.
#
# It reads logs from a file argument or stdin and NEVER modifies state. The
# diagnostic events are already redacted at the source (no token, headers,
# argument values, or payloads); this script additionally refuses to print any
# line containing an Authorization header or a token-looking value, as defense in
# depth.
#
# Usage:
#   ./scripts/diagnose-github-selection.sh [LOGFILE] [--run RUN_ID | --request REQUEST_ID]
#   docker compose logs --no-color backend | ./scripts/diagnose-github-selection.sh --run <run_id>
set -euo pipefail

FILTER_KEY=""
FILTER_VAL=""
LOGFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --run) FILTER_KEY="run_id"; FILTER_VAL="${2:-}"; shift 2 ;;
    --request) FILTER_KEY="request_id"; FILTER_VAL="${2:-}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) LOGFILE="$1"; shift ;;
  esac
done

src() { if [ -n "${LOGFILE}" ]; then cat "${LOGFILE}"; else cat; fi; }

# Only the diagnostic events; strip any line that looks like it carries a secret.
diag() {
  src \
    | grep -F '"logger": "agent.diagnostics"' \
    | grep -viE 'authorization|bearer |ghp_[a-z0-9]|github_pat_'
}

if command -v jq >/dev/null 2>&1; then
  EVENTS='["agent.runtime_path_selected","agent.capability_candidates","agent.planner_candidates","agent.plan_created","agent.plan_tool_resolved","agent.capability_selected","agent.tool_binding_resolved","agent.mcp_tool_invoked","agent.mcp_tool_completed"]'
  diag | jq -c --arg k "${FILTER_KEY}" --arg v "${FILTER_VAL}" --argjson ev "${EVENTS}" '
    select(.event as $e | $ev | index($e))
    | select($k == "" or .[$k] == $v or .request_id == $v)
    | if .event == "agent.capability_candidates"
        then {event, run_id, path, ranking: [.candidates[] | {rank, capability_id, mcp_tool_name, final_score}]}
      elif .event == "agent.runtime_path_selected" then {event, run_id, path, intent_labels}
      elif .event == "agent.capability_selected" then {event, run_id, path, capability_id, mcp_tool_name, final_score}
      elif .event == "agent.tool_binding_resolved" then {event, run_id, capability_id, server_id, mcp_tool_name, binding_lookup_success}
      elif .event == "agent.plan_created" then {event, run_id, task_count, tasks}
      elif .event == "agent.plan_tool_resolved" then {event, run_id, task_id, resolved_capability, executed_capability}
      elif .event == "agent.mcp_tool_invoked" then {event, run_id, server_id, tool_name, connector_status, argument_keys}
      elif .event == "agent.mcp_tool_completed" then {event, run_id, tool_name, success, item_count, error_code, retry_count}
      else {event, run_id} end'
else
  echo "# jq not found — printing raw (already-redacted) diagnostic lines." >&2
  if [ -n "${FILTER_VAL}" ]; then diag | grep -F "${FILTER_VAL}"; else diag; fi
fi
