"""Validation result models shared by the validation pipeline.

Phase 4 uses these for the structural validator; later phases (policy, deps)
reuse the same report shape. See docs/architecture/v2.md §8.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    severity: ValidationSeverity
    step_id: str | None = None
    field: str | None = None


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == ValidationSeverity.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == ValidationSeverity.WARNING for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == ValidationSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == ValidationSeverity.WARNING)
