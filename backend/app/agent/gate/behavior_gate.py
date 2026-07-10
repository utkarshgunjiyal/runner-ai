"""Behavior Gate (Phase 12).

After the context layer builds a RunContext, the gate makes one deterministic
decision: can this request use the DIRECT path, or must it go through the
PLANNER path? It decides *path only* — it does not choose tools, retrieve, or
execute anything.

Deterministic, self-contained, and config-free: keyword/heuristic classification
over the user request plus light signals from working context. No LLM, no DB, no
application settings. See backend/app/agent/ARCHITECTURE.md §10-11.
"""

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    RunContext,
    WorkingContextItem,
)


class BehaviorType(str, Enum):
    DOCUMENT_QA = "document_qa"
    JOB_STATUS = "job_status"
    PREFERENCE_UPDATE = "preference_update"
    MEMORY_QUESTION = "memory_question"
    GENERAL_CHAT = "general_chat"
    MULTI_STEP = "multi_step"
    ACTION = "action"
    COMPARE_ACTION = "compare_action"
    AMBIGUOUS_COMPLEX = "ambiguous_complex"


class EstimatedComplexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class BehaviorDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: BehaviorPath
    behavior_type: BehaviorType
    confidence: float
    reason: str
    requires_planner: bool
    requires_external_capabilities: bool
    estimated_complexity: EstimatedComplexity
    estimated_steps: int
    signals: dict = Field(default_factory=dict)


# --- Keyword sets (lowercased) --------------------------------------------- #

_MULTISTEP = [
    "and then", "after that", "afterwards", "followed by", "once you",
    "once that", "then i", "then we", "and after", "and finally", "next step",
]
_ACTIONS = [
    "schedule", "email", "send", "book", "cancel", "create draft", "draft",
    "reply", "forward", "reschedule", "delete", "invite", "notify", "remind",
    "order", "pay", "publish", "submit", "set up", "sign up", "dispatch", "message",
]
_EXTERNAL = [
    "calendar", "gmail", "inbox", "slack", "meeting", "jira", "ticket",
    "notion", "spreadsheet", "zoom", "teams", "google calendar",
]
_STATUS = [
    "status", "progress", "is it done", "is it ready", "done processing",
    "processing", "how far", "finished", "job status", "job", "ingestion",
    "in progress",
]
_PREFERENCE = [
    "remember", "prefer", "from now on", "forget", "i like", "do not like",
    "don't like", "i prefer", "please remember",
]
_SUMMARIZE = [
    "what is", "what are", "what does", "summarize", "summary", "explain",
    "tell me about", "define", "who is", "how does", "document", "the pdf",
    "the file", "this resume", "the resume", "page", "overview",
]
_MEMORY = [
    "what did we", "did we discuss", "what have we", "did we decide",
    "we talked about", "we discussed", "earlier",
]
_COMPARE = ["compare", "versus", "vs", "difference between", "differences between", "compared to"]

_EXECUTION_SOURCES = {"active_execution_state", "execution_state", "execution"}


def _compile(words):
    return [(w, re.compile(r"\b" + re.escape(w) + r"\b")) for w in words]


_COMPILED = {
    "multistep": _compile(_MULTISTEP),
    "actions": _compile(_ACTIONS),
    "external": _compile(_EXTERNAL),
    "status": _compile(_STATUS),
    "preference": _compile(_PREFERENCE),
    "summarize": _compile(_SUMMARIZE),
    "memory": _compile(_MEMORY),
    "compare": _compile(_COMPARE),
}


def _matches(text: str, compiled) -> list[str]:
    return [word for word, rx in compiled if rx.search(text)]


class BehaviorGate:
    def classify(
        self,
        user_request: str,
        working_context: list[WorkingContextItem] | None = None,
        metadata: dict | None = None,
    ) -> BehaviorDecision:
        text = (user_request or "").lower()
        working_context = working_context or []

        actions = _matches(text, _COMPILED["actions"])
        external = _matches(text, _COMPILED["external"])
        multistep = _matches(text, _COMPILED["multistep"])
        compare = _matches(text, _COMPILED["compare"])
        status = _matches(text, _COMPILED["status"])
        preference = _matches(text, _COMPILED["preference"])
        summarize = _matches(text, _COMPILED["summarize"])
        memory = _matches(text, _COMPILED["memory"])

        has_active_execution = any(
            item.source in _EXECUTION_SOURCES for item in working_context
        )
        word_count = len(text.split())
        clause_markers = (
            text.count(",") + text.count(";") + text.count(" and ") + text.count("?")
        )
        compare_action = bool(compare) and bool(actions)

        signals: dict = {"word_count": word_count}
        for name, matched in (
            ("action_verbs", actions),
            ("external_systems", external),
            ("multi_step", multistep),
            ("compare", compare),
            ("status", status),
            ("preference", preference),
            ("summarize", summarize),
            ("memory", memory),
        ):
            if matched:
                signals[name] = sorted(set(matched))
        if has_active_execution:
            signals["active_execution"] = True

        # -- Planner path: any action / external / multi-step / compare+action
        planner_reasons = []
        if actions:
            planner_reasons.append("action_verbs")
        if external:
            planner_reasons.append("external_systems")
        if multistep:
            planner_reasons.append("multi_step_conjunction")
        if compare_action:
            planner_reasons.append("compare_and_action")

        if planner_reasons:
            num_actions = len(set(actions))
            num_multistep = len(set(multistep))
            steps = max(2, num_actions + num_multistep + (1 if compare_action else 0))

            if compare_action:
                behavior_type = BehaviorType.COMPARE_ACTION
            elif actions and multistep:
                behavior_type = BehaviorType.MULTI_STEP
            else:
                behavior_type = BehaviorType.ACTION

            strong = compare_action or (actions and multistep) or (actions and external)
            complexity = (
                EstimatedComplexity.HIGH
                if (steps >= 3 or (actions and multistep) or compare_action)
                else EstimatedComplexity.MEDIUM
            )
            return BehaviorDecision(
                path=BehaviorPath.PLANNER,
                behavior_type=behavior_type,
                confidence=0.9 if strong else 0.75,
                reason="planner path: " + ", ".join(planner_reasons),
                requires_planner=True,
                requires_external_capabilities=bool(actions or external),
                estimated_complexity=complexity,
                estimated_steps=steps,
                signals=signals,
            )

        # -- Direct path: clear single-capability intents
        if status:
            return self._direct(
                BehaviorType.JOB_STATUS, 0.9, "job/status question", signals
            )
        if preference:
            return self._direct(
                BehaviorType.PREFERENCE_UPDATE, 0.9, "preference update", signals
            )
        if memory:
            return self._direct(
                BehaviorType.MEMORY_QUESTION, 0.85, "conversation memory question", signals
            )
        if summarize:
            return self._direct(
                BehaviorType.DOCUMENT_QA, 0.85, "simple document Q&A / explain", signals
            )

        # -- Uncertain: high complexity -> planner; else general chat direct
        high_complexity = word_count >= 30 or clause_markers >= 3 or has_active_execution
        if high_complexity:
            return BehaviorDecision(
                path=BehaviorPath.PLANNER,
                behavior_type=BehaviorType.AMBIGUOUS_COMPLEX,
                confidence=0.55,
                reason="uncertain, high-complexity request",
                requires_planner=True,
                requires_external_capabilities=False,
                estimated_complexity=EstimatedComplexity.HIGH,
                estimated_steps=2,
                signals=signals,
            )
        return self._direct(
            BehaviorType.GENERAL_CHAT, 0.65, "simple general chat", signals
        )

    def decide(self, run_context: RunContext, attach: bool = True) -> BehaviorDecision:
        decision = self.classify(
            run_context.user_request,
            working_context=run_context.working_context,
            metadata=run_context.metadata,
        )
        if attach:
            run_context.attach_behavior_profile(
                BehaviorProfile(
                    path=decision.path,
                    reason=decision.reason,
                    confidence=decision.confidence,
                )
            )
            run_context.metadata["behavior_decision"] = decision.model_dump()
        return decision

    @staticmethod
    def _direct(behavior_type, confidence, reason, signals) -> BehaviorDecision:
        return BehaviorDecision(
            path=BehaviorPath.DIRECT,
            behavior_type=behavior_type,
            confidence=confidence,
            reason=reason,
            requires_planner=False,
            requires_external_capabilities=False,
            estimated_complexity=EstimatedComplexity.LOW,
            estimated_steps=1,
            signals=signals,
        )
