from __future__ import annotations

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings", "recommended_actions"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "needs_work", "unsafe"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "explanation"],
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line_hint": {"type": "string"},
                    "explanation": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
    },
}


def build_review_prompt(diff: str, base_ref: str) -> str:
    return (
        "Review the current git diff as an advisory reviewer. Do not assume shell access and do not "
        "modify files. Return only JSON that matches the supplied schema. Focus on correctness, "
        "security, data loss, and test gaps.\n\n"
        f"Base ref: {base_ref}\n\n"
        "Diff:\n"
        "```diff\n"
        f"{diff}\n"
        "```"
    )

