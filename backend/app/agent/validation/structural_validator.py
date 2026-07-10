"""Deterministic structural validator for Plan objects.

Answers "is this plan well-formed and executable against the registry?" — NOT
"should it be allowed?" (that is the Policy Engine, a later phase). Collects all
issues; never fails fast. Pure, side-effect-free: does not mutate the Plan or
the ToolRegistry. See docs/architecture/v2.md §8.
"""

import re

from app.agent.models.plan import Plan, PlanStep, PlanStepType
from app.agent.models.tool_spec import ToolSpec
from app.agent.models.validation import (
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
)
from app.agent.registry.registry import ToolRegistry

# Matches a whole-string binding: "${ ... }"
_BINDING_RE = re.compile(r"^\$\{([^}]*)\}$")

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def _looks_like_binding(value) -> bool:
    return isinstance(value, str) and value.strip().startswith("${")


def _parse_binding(value: str) -> tuple[str, str] | None:
    match = _BINDING_RE.match(value.strip())
    if not match:
        return None
    inner = match.group(1).strip()
    if "." not in inner:
        return None
    step_id, path = inner.split(".", 1)
    step_id, path = step_id.strip(), path.strip()
    if not step_id or not path:
        return None
    return step_id, path


class StructuralPlanValidator:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate(self, plan: Plan) -> ValidationReport:
        issues: list[ValidationIssue] = []
        positions = {step.id: i for i, step in enumerate(plan.steps)}

        for index, step in enumerate(plan.steps):
            self._validate_capability_and_args(step, issues)
            self._validate_dependencies(step, positions, issues)
            self._validate_bindings(step, index, plan, positions, issues)

        return ValidationReport(issues=issues)

    # -- capability + args ---------------------------------------------------

    def _validate_capability_and_args(self, step: PlanStep, issues: list) -> None:
        if step.step_type != PlanStepType.TOOL:
            return  # FINAL_RESPONSE steps do not require a capability_id

        if not step.capability_id:
            issues.append(
                ValidationIssue(
                    code="MISSING_CAPABILITY_ID",
                    message=f"TOOL step '{step.id}' has no capability_id",
                    severity=ValidationSeverity.ERROR,
                    step_id=step.id,
                )
            )
            return

        if not self._registry.exists(step.capability_id):
            issues.append(
                ValidationIssue(
                    code="UNKNOWN_CAPABILITY",
                    message=f"capability '{step.capability_id}' is not in the registry",
                    severity=ValidationSeverity.ERROR,
                    step_id=step.id,
                    field="capability_id",
                )
            )
            return

        tool = self._registry.get(step.capability_id)
        if not tool.enabled:
            issues.append(
                ValidationIssue(
                    code="DISABLED_TOOL",
                    message=f"capability '{tool.id}' is disabled",
                    severity=ValidationSeverity.ERROR,
                    step_id=step.id,
                    field="capability_id",
                )
            )

        self._validate_args(step, tool, issues)

    def _validate_args(self, step: PlanStep, tool: ToolSpec, issues: list) -> None:
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for req in required:
            if req not in step.args:
                issues.append(
                    ValidationIssue(
                        code="MISSING_REQUIRED_ARG",
                        message=f"step '{step.id}' missing required arg '{req}'",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                        field=req,
                    )
                )

        for name, value in step.args.items():
            if properties and name not in properties:
                # Assumption: unknown args are a WARNING (forward-compatible /
                # pass-through), not a hard error.
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_ARG",
                        message=f"step '{step.id}' has unknown arg '{name}'",
                        severity=ValidationSeverity.WARNING,
                        step_id=step.id,
                        field=name,
                    )
                )
                continue

            if _looks_like_binding(value):
                continue  # value resolved at execution time; skip type check

            prop = properties.get(name, {})
            expected = prop.get("type") if isinstance(prop, dict) else None
            if expected in _TYPE_CHECKS and value is not None:
                if not _TYPE_CHECKS[expected](value):
                    issues.append(
                        ValidationIssue(
                            code="WRONG_ARG_TYPE",
                            message=(
                                f"step '{step.id}' arg '{name}' expected {expected}"
                            ),
                            severity=ValidationSeverity.ERROR,
                            step_id=step.id,
                            field=name,
                        )
                    )

    # -- dependencies (defensive; Plan model already enforces these) ---------

    def _validate_dependencies(self, step: PlanStep, positions: dict, issues: list) -> None:
        for dep in step.depends_on:
            if dep == step.id:
                issues.append(
                    ValidationIssue(
                        code="SELF_DEPENDENCY",
                        message=f"step '{step.id}' depends on itself",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                    )
                )
            elif dep not in positions:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_DEPENDENCY",
                        message=f"step '{step.id}' depends on unknown step '{dep}'",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                    )
                )

    # -- bindings ------------------------------------------------------------

    def _validate_bindings(
        self, step: PlanStep, index: int, plan: Plan, positions: dict, issues: list
    ) -> None:
        for name, value in step.args.items():
            if not _looks_like_binding(value):
                continue

            parsed = _parse_binding(value)
            if parsed is None:
                issues.append(
                    ValidationIssue(
                        code="MALFORMED_BINDING",
                        message=f"step '{step.id}' arg '{name}' has a malformed binding: {value}",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                        field=name,
                    )
                )
                continue

            ref_id, path = parsed

            if ref_id not in positions:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_BINDING_STEP",
                        message=f"step '{step.id}' arg '{name}' binds to unknown step '{ref_id}'",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                        field=name,
                    )
                )
                continue

            if positions[ref_id] >= index:
                issues.append(
                    ValidationIssue(
                        code="BINDING_FORWARD_REFERENCE",
                        message=f"step '{step.id}' arg '{name}' binds to non-prior step '{ref_id}'",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                        field=name,
                    )
                )

            if not path.startswith("output."):
                issues.append(
                    ValidationIssue(
                        code="BINDING_INVALID_PATH",
                        message=f"step '{step.id}' arg '{name}' binding path must start with 'output.'",
                        severity=ValidationSeverity.ERROR,
                        step_id=step.id,
                        field=name,
                    )
                )
                continue

            self._validate_binding_output_field(step, name, ref_id, path, plan, issues)

    def _validate_binding_output_field(
        self, step: PlanStep, arg_name: str, ref_id: str, path: str, plan: Plan, issues: list
    ) -> None:
        field_name = path[len("output."):].split(".")[0]
        if not field_name:
            return

        ref_step = plan.get_step(ref_id)
        if ref_step.step_type != PlanStepType.TOOL or not ref_step.capability_id:
            return
        if not self._registry.exists(ref_step.capability_id):
            return  # unknown capability already reported on the referenced step

        ref_tool = self._registry.get(ref_step.capability_id)
        output_props = ref_tool.output_schema.get("properties", {}) if isinstance(
            ref_tool.output_schema, dict
        ) else {}
        available = set(ref_tool.output_fields) | set(output_props.keys())

        if available and field_name not in available:
            issues.append(
                ValidationIssue(
                    code="BINDING_UNKNOWN_OUTPUT_FIELD",
                    message=(
                        f"step '{step.id}' arg '{arg_name}' binds to output field "
                        f"'{field_name}' not produced by '{ref_id}'"
                    ),
                    severity=ValidationSeverity.ERROR,
                    step_id=step.id,
                    field=arg_name,
                )
            )
