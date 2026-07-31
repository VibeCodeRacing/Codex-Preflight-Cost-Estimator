#!/usr/bin/env python3

import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time


MODEL_RATES = {
    "Sol": {"input": 125.0, "cached": 12.5, "output": 750.0},
    "Terra": {"input": 50.0, "cached": 5.0, "output": 300.0},
    "Luna": {"input": 5.0, "cached": 0.5, "output": 30.0},
}

ESTIMATOR_VERSION = 1
MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024
RATE_CARD_DATE = "2026-07-31"

TASK_SCENARIOS = {
    "Luna": {
        "label": "narrow",
        "context": (6_000, 16_000),
        "output": (300, 2_500),
        "calls": (1, 2),
    },
    "Terra": {
        "label": "standard",
        "context": (8_000, 28_000),
        "output": (800, 7_000),
        "calls": (1, 5),
    },
    "Sol": {
        "label": "complex",
        "context": (12_000, 50_000),
        "output": (2_000, 20_000),
        "calls": (2, 12),
    },
}


def approval_key(payload):
    material = "\0".join(
        (
            str(payload.get("session_id", "")),
            str(payload.get("prompt", "")),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def estimate_draft_tokens(text):
    encoded_length = len(text.encode("utf-8"))
    structural_count = sum(character in "{}[]():,.;/\\=_-+*#<>" for character in text)
    midpoint = max(1.0, (encoded_length / 3.9) + (structural_count * 0.08))
    low = max(1, math.floor(midpoint * 0.80))
    high = max(low + 1, math.ceil(midpoint * 1.25))
    return low, high


def recommend_model(prompt):
    normalized = prompt.casefold()
    strong_complex_signals = (
        "duplicate charges",
        "zero-downtime",
        "production race",
        "production incident",
        "security audit",
        "data loss",
        "irreversible",
    )
    complex_signals = (
        "architecture",
        "race condition",
        "vulnerability",
        "data migration",
        "intermittent",
        "root cause",
        "across multiple services",
        "distributed system",
        "concurrency",
        "irreversible",
    )
    simple_signals = (
        "format",
        "translate",
        "rewrite",
        "summarize",
        "extract",
        "markdown table",
        "sort this",
        "fix this typo",
        "rename",
        "replace ",
        "correct ",
        "change nothing else",
        "fix the spelling",
    )

    complex_score = sum(signal in normalized for signal in complex_signals)
    if (
        any(signal in normalized for signal in strong_complex_signals)
        or complex_score >= 2
        or len(prompt) >= 4_000
    ):
        return "Sol"
    if len(prompt) <= 800 and any(signal in normalized for signal in simple_signals):
        return "Luna"
    return "Terra"


def credit_cost(input_tokens, cached_fraction, output_tokens, rates):
    cached_tokens = input_tokens * cached_fraction
    uncached_tokens = input_tokens - cached_tokens
    return (
        (uncached_tokens * rates["input"])
        + (cached_tokens * rates["cached"])
        + (output_tokens * rates["output"])
    ) / 1_000_000


def estimate_credit_ranges(draft_tokens, recommendation, last_usage=None):
    draft_low, draft_high = draft_tokens
    scenario = TASK_SCENARIOS[recommendation]
    context_low, context_high = scenario["context"]
    output_low, output_high = scenario["output"]
    calls_low, calls_high = scenario["calls"]

    cached_fraction_low_cost = 0.90
    cached_fraction_high_cost = 0.30
    if last_usage:
        previous_input = max(0, int(last_usage.get("input_tokens", 0)))
        previous_cached = max(0, int(last_usage.get("cached_input_tokens", 0)))
        if previous_input:
            context_low = max(context_low, math.floor(previous_input * 0.85))
            context_high = max(context_high, math.ceil(previous_input * 1.35))
            previous_cached_fraction = min(1.0, previous_cached / previous_input)
            cached_fraction_low_cost = min(
                0.98, max(0.50, previous_cached_fraction + 0.05)
            )
            cached_fraction_high_cost = min(
                0.80, max(0.05, previous_cached_fraction - 0.25)
            )

    low_input = (context_low + draft_low) * calls_low
    high_input = (context_high + draft_high) * calls_high
    low_output = output_low * calls_low
    high_output = output_high * calls_high

    ranges = {}
    for model_name, rates in MODEL_RATES.items():
        ranges[model_name] = (
            credit_cost(
                low_input, cached_fraction_low_cost, low_output, rates
            ),
            credit_cost(
                high_input, cached_fraction_high_cost, high_output, rates
            ),
        )
    return ranges


def display_model(model_slug):
    normalized = str(model_slug).casefold()
    for model_name in MODEL_RATES:
        if model_name.casefold() in normalized:
            return model_name
    return str(model_slug) or "Unknown"


def format_credit(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def read_last_token_usage(transcript_path):
    if not transcript_path:
        return None
    path = Path(str(transcript_path))
    if not path.is_file():
        return None

    latest = None
    with path.open("rb") as transcript:
        size = path.stat().st_size
        start = max(0, size - MAX_TRANSCRIPT_BYTES)
        transcript.seek(start)
        if start:
            transcript.readline()
        for raw_line in transcript:
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload", {})
            if payload.get("type") != "token_count":
                continue
            candidate = payload.get("info", {}).get("last_token_usage")
            if isinstance(candidate, dict):
                latest = candidate
    return latest


def configured_duration(value):
    return str(int(value)) if value.is_integer() else f"{value:g}"


def block_reason(payload, ttl_seconds):
    prompt = str(payload.get("prompt", ""))
    draft_tokens = estimate_draft_tokens(prompt)
    recommendation = recommend_model(prompt)
    last_usage = read_last_token_usage(payload.get("transcript_path", ""))
    credit_ranges = estimate_credit_ranges(
        draft_tokens, recommendation, last_usage=last_usage
    )
    selected = display_model(payload.get("model", ""))

    lines = [
        f"Cost preflight (heuristic; standard credits as of {RATE_CARD_DATE})",
        (
            f"Draft: {draft_tokens[0]:,}-{draft_tokens[1]:,} tokens | "
            f"Task: {TASK_SCENARIOS[recommendation]['label']}"
        ),
        f"Selected: {selected} | Recommended: {recommendation}",
    ]
    for model_name in ("Sol", "Terra", "Luna"):
        low, high = credit_ranges[model_name]
        lines.append(
            f"{model_name}: {format_credit(low)}-{format_credit(high)} credits"
        )
    lines.extend(
        (
            "Includes estimated context, output, and agent loops; Fast mode and "
            "attachments may cost more.",
            "Submit the same prompt again within "
            f"{configured_duration(ttl_seconds)} seconds to continue.",
            "No model request was sent.",
        )
    )
    return "\n".join(lines)


def duplicate_reason():
    return (
        "This prompt was already released by cost preflight. "
        "Wait for the active turn to start or edit the prompt before trying again.\n"
        "No additional model request was sent."
    )


def load_state(state_file):
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def write_state(state_file, state):
    descriptor, temporary_name = tempfile.mkstemp(
        dir=state_file.parent, prefix=".pending-", suffix=".json"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(state_file)
        state_file.chmod(0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def remove_stale_state_files(state_dir, now, retention_seconds):
    for candidate in state_dir.glob("*.json"):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if now - candidate.stat().st_mtime > retention_seconds:
                candidate.unlink()
        except FileNotFoundError:
            continue


def emit_block(reason):
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("hook input must be an object")
    if payload.get("hook_event_name") not in (None, "UserPromptSubmit"):
        return False
    if not isinstance(payload.get("prompt"), str):
        raise ValueError("prompt must be a string")
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
        raise ValueError("session_id must be a non-empty string")
    return True


def prepare_state_directory(state_dir):
    if state_dir.exists():
        if state_dir.is_symlink() or not state_dir.is_dir():
            raise PermissionError("state path is not a private directory")
        if stat.S_IMODE(state_dir.stat().st_mode) & 0o077:
            raise PermissionError("existing state directory is not private")
        return
    state_dir.mkdir(parents=True, mode=0o700)
    state_dir.chmod(0o700)


def run_hook():
    payload = json.load(sys.stdin)
    if not validate_payload(payload):
        return 0
    state_dir = Path(
        os.environ.get(
            "CODEX_COST_PREFLIGHT_STATE_DIR",
            str(Path.home() / ".codex" / "cost-preflight" / "state"),
        )
    )
    prepare_state_directory(state_dir)

    now = time.time()
    ttl_seconds = float(os.environ.get("CODEX_COST_PREFLIGHT_TTL_SECONDS", "90"))
    min_delay_seconds = float(
        os.environ.get("CODEX_COST_PREFLIGHT_MIN_CONFIRM_DELAY_SECONDS", "1")
    )
    if ttl_seconds <= 0 or min_delay_seconds < 0 or min_delay_seconds >= ttl_seconds:
        raise ValueError("confirmation timing is invalid")
    state_file = state_dir / f"{approval_key(payload)}.json"
    lock_file = state_dir / ".lock"

    with lock_file.open("a+", encoding="utf-8") as lock:
        lock_file.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        remove_stale_state_files(
            state_dir, now, retention_seconds=max(ttl_seconds * 4, 3_600)
        )
        state = load_state(state_file)
        if state:
            age = now - float(state.get("created_at", 0))
            if state.get("status") == "consumed" and age <= ttl_seconds:
                emit_block(duplicate_reason())
                return 0
            if (
                state.get("status") == "pending"
                and min_delay_seconds <= age <= ttl_seconds
            ):
                state["status"] = "consumed"
                state["consumed_at"] = now
                write_state(state_file, state)
                return 0
            if state.get("status") == "pending" and age < min_delay_seconds:
                emit_block(block_reason(payload, ttl_seconds))
                return 0

        write_state(
            state_file,
            {
                "created_at": now,
                "status": "pending",
                "version": ESTIMATOR_VERSION,
            },
        )
        emit_block(block_reason(payload, ttl_seconds))
    return 0


def main():
    try:
        return run_hook()
    except Exception as error:
        response = {
            "continue": True,
            "systemMessage": (
                "Cost preflight unavailable "
                f"({type(error).__name__}); the prompt was not blocked."
            ),
        }
        sys.stdout.write(json.dumps(response))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
