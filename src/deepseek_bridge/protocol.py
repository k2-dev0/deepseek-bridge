"""Bounded public contracts. Untrusted diagnostics never become error messages."""

import json
import os
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

type ErrorClass = Literal[
    "configuration_error",
    "privacy_configuration_error",
    "authentication_error",
    "transport_error",
    "harness_start_error",
    "harness_protocol_error",
    "model_error",
    "task_contract_error",
    "abort_error",
    "internal_error",
]
type Status = Literal["running", "completed", "needs_decision", "failed", "aborted", "interrupted"]

MESSAGES: dict[ErrorClass, str] = {
    "configuration_error": "Invalid configuration, input, task ID, or task state.",
    "privacy_configuration_error": "The mandatory privacy configuration is unavailable or invalid.",
    "authentication_error": "DeepSeek authentication was rejected.",
    "transport_error": "The model endpoint could not complete the request.",
    "harness_start_error": "The pinned Harness runtime could not initialize.",
    "harness_protocol_error": "The Harness runtime or model stream violated the protocol.",
    "model_error": "The model did not complete the requested task.",
    "task_contract_error": "The final response violates the bounded JSON result contract.",
    "abort_error": "Runtime cleanup failed; no new writer will be accepted.",
    "internal_error": "An internal worker error occurred.",
}


class BridgeError(Exception):
    def __init__(self, category: ErrorClass):
        self.category = category
        super().__init__(category + ": " + MESSAGES[category])

    def as_dict(self) -> dict[str, str]:
        return {"class": self.category, "message": MESSAGES[self.category]}


_CREDENTIAL = re.compile(
    r"(?:\b[A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*\S+)"
    r"|(?:\b(?:sk-|gh[pousr]_|github_pat_|AKIA)[A-Za-z0-9_/-]{12,})"
    r"|(?:authorization\s*:)|(?:bearer\s+\S+)|(?:-----BEGIN .*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def contains_credential(text: str) -> bool:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return bool((key and key in text) or _CREDENTIAL.search(text))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


type Brief = Annotated[str, Field(min_length=1, max_length=32000)]
type TaskID = Annotated[str, Field(min_length=1, max_length=80)]


class StartInput(StrictModel):
    brief: Brief
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @field_validator("brief", "title")
    @classmethod
    def safe_text(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or contains_credential(value)):
            raise ValueError("Empty or credential-like input is not accepted")
        return value


class WaitInput(StrictModel):
    task_id: TaskID
    timeout_ms: Annotated[int, Field(ge=0, le=60000)] = 60000


class ContinueInput(StrictModel):
    task_id: TaskID
    message: Brief

    @field_validator("message")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if not value.strip() or contains_credential(value):
            raise ValueError("Empty or credential-like input is not accepted")
        return value


class AbortInput(StrictModel):
    task_id: TaskID


type ShortText = Annotated[str, Field(min_length=1, max_length=500)]
type ShortList = Annotated[list[ShortText], Field(max_length=50)]


class FinalResponse(StrictModel):
    status: Literal["completed", "needs_decision", "failed"]
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    tests: ShortList
    question: Annotated[str, Field(min_length=1, max_length=1000)] | None
    affected_paths: ShortList
    unresolved: ShortList

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status == "needs_decision" and not (self.question and self.question.strip()):
            raise ValueError("Only needs_decision has a nonempty question")
        if self.status != "needs_decision" and self.question is not None:
            raise ValueError("Only needs_decision has a question")
        if not self.summary.strip():
            raise ValueError("A summary is required")
        for value in self.affected_paths:
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or PureWindowsPath(value).is_absolute()
                or ".." in path.parts
                or "\\" in value
                or not path.parts
            ):
                raise ValueError("Affected paths must be relative to the repository")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_final(response: str) -> FinalResponse:
    try:
        if len(response) > 16000 or contains_credential(response):
            raise ValueError("Response exceeds the contract")
        value = json.loads(response, object_pairs_hook=_unique_object)
        if contains_credential(json.dumps(value, ensure_ascii=False)):
            raise ValueError("Decoded response contains a credential")
        return FinalResponse.model_validate(value)
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise BridgeError("task_contract_error") from None


COMMON_INSTRUCTIONS = """You are a repository worker. First read AGENTS.md at the repository root.
Read only referenced documents needed for this task. Preserve existing user changes.
Investigate, edit, test, typecheck and lint within this repository.
Never stage, commit, branch, rebase, reset, checkout, restore, clean, or write .git.
Do not change agent configuration, secrets, credentials or lockfiles
without an explicit requirement.
Return needs_decision when a design decision is required instead of guessing.
Do not send external messages, publish, deploy, or perform billing actions.
Return ONLY a JSON object, without Markdown fences, with exactly these required fields:
status: "completed", "needs_decision" or "failed";
summary: nonempty string, at most 2000 characters;
tests: list of at most 50 strings, each 1..500 characters, state what actually ran;
question: a nonempty string of at most 1000 characters for needs_decision, otherwise null;
affected_paths: at most 50 repository-relative paths, each 1..500 characters, no parent traversal;
unresolved: list of at most 50 strings, each 1..500 characters.
The entire JSON must be at most 16000 characters. Do not include credentials, file contents,
full tool results, or full conversations. Summarize changes, verification and unresolved issues.
"""
