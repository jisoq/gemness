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


def build_review_prompt(base_ref: str) -> str:
    return (
        "Review the current repository changes as an advisory reviewer. Gemness has not embedded a diff; "
        "use Antigravity CLI's own repository inspection capabilities from the current working directory "
        "to inspect changed files and compare them with the requested base reference as needed. Do not "
        "modify files, do not read or quote secrets/private keys/raw environment values, and return only "
        "JSON that matches the supplied schema. Focus on correctness, security, data loss, and test gaps.\n\n"
        f"Base ref: {base_ref}"
    )
