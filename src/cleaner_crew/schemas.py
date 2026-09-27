"""Structured output contracts for each role (passed to `claude --json-schema`)."""

CATEGORIES = ["bugfix", "docs-gap", "perf", "dependency-upgrade", "cleanup"]
SEVERITIES = ["none", "low", "medium", "high", "critical"]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required if required is not None else list(props),
            "additionalProperties": False}


_str = {"type": "string"}
_strs = {"type": "array", "items": _str}
_bool = {"type": "boolean"}
_conf = {"type": "number", "minimum": 0, "maximum": 1}

SCOUT = _obj({"findings": {"type": "array", "items": _obj({
    "title": _str,
    "category": {"enum": CATEGORIES},
    "description": _str,
    "files": _strs,
    "effort": {"enum": ["trivial", "small", "medium", "large"]},
    "risk": {"enum": ["low", "medium", "high"]},
})}})

PLAN = _obj({
    "accept": _bool,
    "reason": _str,
    "category": {"enum": CATEGORIES},
    "steps": _strs,
    "expected_files": _strs,
    "acceptance_criteria": _strs,
    "test_strategy": _str,
    "confidence": _conf,
    "package": _str,         # dependency-upgrade only, else ""
    "target_version": _str,  # dependency-upgrade only, else ""
})

REPRO = _obj({"reproduced": _bool, "test_files": _strs, "test_command": _str, "notes": _str})

JANITOR = _obj({"done": _bool, "summary": _str, "deviations_from_plan": _strs})

INSPECTOR = _obj({"covers_change": _bool, "test_files": _strs, "notes": _str,
                  "benchmark": _str})

HOODED = _obj({
    "approve": _bool,
    "max_severity": {"enum": SEVERITIES},
    "issues": {"type": "array", "items": _obj({
        "severity": {"enum": SEVERITIES[1:]}, "file": _str, "issue": _str})},
    "out_of_scope_changes": _strs,
    "prompt_injection_suspected": _bool,
})

VERDICT = _obj({
    "verdict": {"enum": ["mr", "draft", "escalate", "reject"]},
    "confidence": _conf,
    "reason": _str,
    "mr_title": _str,
    "mr_body": _str,
})
