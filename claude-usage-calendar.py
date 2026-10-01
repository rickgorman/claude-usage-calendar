#!/usr/bin/env python3
"""
AI Agent Token Usage Calendar Generator

Parses Claude, Codex, Grok, and Composer session files and generates an
interactive HTML calendar visualization of token usage with multiple views.

Usage:
    ./claude-usage-calendar.py
    ./claude-usage-calendar.py --utc
    ./claude-usage-calendar.py --tz-offset -8
    ./claude-usage-calendar.py --no-open
    ./claude-usage-calendar.py --output /tmp/x.html
"""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT_NAMES = ("claude", "codex", "grok", "composer")
AGENT_DISPLAY_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "grok": "Grok",
    "composer": "Composer",
}
AGENT_COLORS = {
    "claude": "#d97757",
    "codex": "#10a37f",
    "grok": "#4da3ff",
    "composer": "#a855f7",
}
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
UUID_JSONL_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
)
CLAUDE_AGENT_PATTERN = re.compile(r"agent-[0-9a-f]+\.jsonl$")


def empty_usage():
    """Return a new zeroed token usage record."""
    return {field: 0 for field in TOKEN_FIELDS}


def find_session_files(search_path="~/"):
    """Find and classify supported agent JSONL session files."""
    expanded_path = os.path.abspath(os.path.expanduser(search_path))
    home_path = os.path.abspath(os.path.expanduser("~"))
    if expanded_path == home_path:
        search_roots = [
            os.path.join(home_path, directory)
            for directory in (".claude", ".codex", ".grok", ".cursor")
            if os.path.isdir(os.path.join(home_path, directory))
        ]
    else:
        search_roots = [expanded_path]

    if not search_roots:
        return {agent: [] for agent in AGENT_NAMES}

    result = subprocess.run(
        ["find", *search_roots, "-name", "*.jsonl", "-type", "f"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )

    files = {agent: [] for agent in AGENT_NAMES}

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue

        path = line.replace(os.sep, "/")
        basename = os.path.basename(line)

        if basename.startswith("rollout-") and basename.endswith(".jsonl"):
            files["codex"].append(line)
        elif "/.grok/sessions/" in path and basename == "updates.jsonl":
            files["grok"].append(line)
        elif (
            "/.cursor/projects/" in path
            and "/agent-transcripts/" in path
            and UUID_JSONL_PATTERN.fullmatch(basename)
        ):
            files["composer"].append(line)
        elif (
            UUID_JSONL_PATTERN.fullmatch(basename)
            or CLAUDE_AGENT_PATTERN.fullmatch(basename)
        ):
            # Keep the historical UUID/agent fallback for custom search roots.
            files["claude"].append(line)

    return files


def find_jsonl_files(search_path="~/"):
    """Backward-compatible flattened list of all supported JSONL files."""
    files_by_agent = find_session_files(search_path)
    return [path for agent in AGENT_NAMES for path in files_by_agent[agent]]


def parse_timestamp(value, tz, fallback=None):
    """Convert ISO-8601 or Unix timestamps to the requested timezone."""
    if value in (None, ""):
        value = fallback
    if value in (None, ""):
        return None

    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            dt = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)
    except (OSError, TypeError, ValueError):
        return None


def normalize_usage(input_tokens=0, output_tokens=0, cache_read=0, cache_create=0):
    """Normalize token counters to non-negative integers."""
    values = (input_tokens, output_tokens, cache_read, cache_create)
    normalized = []
    for value in values:
        try:
            normalized.append(max(0, int(value or 0)))
        except (TypeError, ValueError):
            normalized.append(0)
    return dict(zip(TOKEN_FIELDS, normalized))


def make_record(agent, record_id, timestamp, usage, estimated=False):
    """Create a normalized parser record, or None when it cannot be dated."""
    if timestamp is None or not any(usage.values()):
        return None
    return {
        "agent": agent,
        "id": f"{agent}:{record_id}",
        "date": timestamp.strftime("%Y-%m-%d"),
        "hour": timestamp.hour,
        "usage": usage,
        "estimated": estimated,
    }


def parse_claude_file(filepath, tz):
    """Yield Claude usage records; callers deduplicate streaming snapshots."""
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
                continue
            message = entry["message"]
            usage = message.get("usage") or {}
            message_id = message.get("id")
            if not message_id or not usage:
                continue
            record = make_record(
                "claude",
                message_id,
                parse_timestamp(entry.get("timestamp"), tz),
                normalize_usage(
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens"),
                    usage.get("cache_creation_input_tokens"),
                ),
            )
            if record:
                yield record


def parse_codex_file(filepath, tz):
    """Yield Codex per-model-call token_count records."""
    previous_total = empty_usage()
    seen_totals = set()
    with open(filepath, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload") or {}
            if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            total_usage = normalize_usage(
                total.get("input_tokens"),
                total.get("output_tokens"),
                total.get("cached_input_tokens"),
                total.get("cache_write_input_tokens"),
            )
            total_snapshot = tuple(total_usage[field] for field in TOKEN_FIELDS)
            if total and total_snapshot in seen_totals:
                continue
            if total:
                seen_totals.add(total_snapshot)
            current = info.get("last_token_usage")
            if current:
                usage = normalize_usage(
                    current.get("input_tokens"),
                    current.get("output_tokens"),
                    current.get("cached_input_tokens"),
                    current.get("cache_write_input_tokens"),
                )
            else:
                usage = {
                    field: max(0, total_usage[field] - previous_total[field])
                    for field in TOKEN_FIELDS
                }
            if total:
                previous_total = total_usage
            record_id = entry.get("ordinal", line_number)
            record = make_record(
                "codex",
                f"{filepath}:{record_id}",
                parse_timestamp(entry.get("timestamp"), tz),
                usage,
            )
            if record:
                yield record


def parse_grok_file(filepath, tz):
    """Yield Grok prompt usage records; callers keep the largest snapshot."""
    with open(filepath, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            params = entry.get("params") or {}
            update = params.get("update") or {}
            usage_data = update.get("usage")
            if not isinstance(usage_data, dict):
                continue
            prompt_id = update.get("prompt_id") or f"line-{line_number}"
            session_id = params.get("sessionId") or os.path.dirname(filepath)
            record = make_record(
                "grok",
                f"{session_id}:{prompt_id}",
                parse_timestamp(
                    entry.get("timestamp"),
                    tz,
                    (params.get("_meta") or {}).get("agentTimestampMs"),
                ),
                normalize_usage(
                    usage_data.get("inputTokens"),
                    usage_data.get("outputTokens"),
                    usage_data.get("cachedReadTokens"),
                    usage_data.get("cacheCreationTokens"),
                ),
            )
            if record:
                yield record


def estimate_tokens(value):
    """Estimate tokens from Cursor transcript content at four characters/token."""
    if value is None or value == "" or value == [] or value == {}:
        return 0
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return math.ceil(len(text) / 4) if text else 0


def parse_composer_file(filepath, tz):
    """Yield estimated Cursor Composer token records from exported transcripts."""
    fallback_timestamp = os.path.getmtime(filepath)
    with open(filepath, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = entry.get("role")
            message = entry.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else message
            tokens = estimate_tokens(content)
            if role == "user":
                usage = normalize_usage(input_tokens=tokens)
            elif role == "assistant":
                usage = normalize_usage(output_tokens=tokens)
            else:
                continue
            record = make_record(
                "composer",
                f"{filepath}:{line_number}",
                parse_timestamp(entry.get("timestamp"), tz, fallback_timestamp),
                usage,
                estimated=True,
            )
            if record:
                yield record


PARSERS = {
    "claude": parse_claude_file,
    "codex": parse_codex_file,
    "grok": parse_grok_file,
    "composer": parse_composer_file,
}


class CostExtractionError(ValueError):
    """A costs-on record cannot be represented without hiding visible usage."""


MERGE_INCONSISTENCY_ASSUMPTION = (
    "streaming snapshots changed exact dimensions; merged maxima unpriced"
)
STREAMING_MAXIMA_ASSUMPTION = (
    "same-dimension streaming snapshots merged by maxima; derived estimate"
)
GROK_CLI_API_EQUIVALENT_ASSUMPTION = (
    "Grok CLI aggregate valued at standard-context published API-equivalent rates; not an invoice"
)
COMPOSER_MODEL_APPROXIMATION_ASSUMPTION = (
    "historical Composer model approximated by current Composer 2.5 Fast published rate"
)
CODEX_CUMULATIVE_REGRESSION_ASSUMPTION = (
    "Codex cumulative counters regressed; current snapshot retained unpriced as reset baseline"
)
NORMALIZATION_INCONSISTENCY_ASSUMPTIONS = frozenset(
    {
        MERGE_INCONSISTENCY_ASSUMPTION,
        CODEX_CUMULATIVE_REGRESSION_ASSUMPTION,
    }
)


def _bounded_record_id(agent, filepath, native_id, *, global_identity=False):
    """Return a stable source ID without exposing an unbounded local path."""
    native = str(native_id)
    if global_identity and native and len(native) <= 240:
        return f"{agent}:{native}"
    path_digest = hashlib.sha256(str(filepath).encode("utf-8")).hexdigest()[:16]
    if len(native) > 190:
        native = hashlib.sha256(native.encode("utf-8")).hexdigest()
    return f"{agent}:{path_digest}:{native}"


def _explicit_api_provenance(*values):
    """Recognize only affirmative API-billing provenance, never a CLI default."""
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in ("billing_channel", "billingChannel", "channel"):
            if str(value.get(key, "")).casefold() == "api":
                return True
        for key in ("auth_mode", "authMode"):
            if str(value.get(key, "")).casefold() in {"api", "api_key", "apikey"}:
                return True
        for key in ("api_billing", "apiBilling", "api_provenance"):
            if value.get(key) is True:
                return True
    return False


def _cost_identifier(value, field_name, assumptions):
    """Sanitize one untrusted dimension without discarding its counters."""
    from costs.contracts import FORBIDDEN_MAPPING_KEYS, MAX_IDENTIFIER_LENGTH

    if value is None:
        return None
    if (
        isinstance(value, str)
        and value
        and len(value) <= MAX_IDENTIFIER_LENGTH
        and value not in FORBIDDEN_MAPPING_KEYS
    ):
        return value
    assumptions.add(f"invalid {field_name} metadata omitted")
    return None


def _safe_cost_source(value, assumptions, depth=0):
    """Keep JSON-shaped source data while dropping only unsafe metadata keys."""
    from costs.contracts import FORBIDDEN_MAPPING_KEYS, MAX_IDENTIFIER_LENGTH

    if depth > 32:
        assumptions.add("over-deep source metadata omitted")
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if (
                not key
                or len(key) > MAX_IDENTIFIER_LENGTH
                or key in FORBIDDEN_MAPPING_KEYS
            ):
                assumptions.add("unsafe source metadata key omitted")
                continue
            result[key] = _safe_cost_source(item, assumptions, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_cost_source(item, assumptions, depth + 1) for item in value]
    assumptions.add("non-JSON source metadata omitted")
    return None


def _cost_record(record_type, **values):
    """Construct a frozen record without letting bad metadata hide counters."""
    from costs.contracts import RawUsageRecord

    assumptions = set(values.pop("assumptions", ()))
    for field_name in (
        "model",
        "channel",
        "variant",
        "service_tier",
        "context_band",
    ):
        values[field_name] = _cost_identifier(
            values.get(field_name), field_name, assumptions
        )
    values["source"] = _safe_cost_source(values.get("source", {}), assumptions)
    values["assumptions"] = tuple(sorted(assumptions))
    try:
        return RawUsageRecord(provider=record_type, **values)
    except (TypeError, ValueError) as error:
        raise CostExtractionError(
            f"cost record {values.get('source_record_id', 'unknown')} cannot be represented"
        ) from error


def _extract_claude_cost_records(filepath, tz):
    from costs.contracts import MeasurementQuality, Provider

    records = []
    with open(filepath, "r", encoding="utf-8") as source_file:
        for line_number, line in enumerate(source_file, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            occurred_at = parse_timestamp(entry.get("timestamp"), tz)
            if not isinstance(usage, dict) or occurred_at is None:
                continue
            native_id = message.get("id") or f"line-{line_number}"
            source_id = _bounded_record_id(
                "claude", filepath, native_id, global_identity=message.get("id") is not None
            )
            channel = (
                "api"
                if _explicit_api_provenance(entry, message, usage)
                else "subscription"
            )
            if channel == "subscription":
                variant = "standard"
                service_tier = "default"
                context_band = "standard"
            else:
                variant = message.get("variant", "standard")
                service_tier = usage.get("service_tier", "default")
                context_band = usage.get("context_band", "standard")
            records.append(_cost_record(
                Provider.ANTHROPIC.value,
                source_record_id=source_id,
                agent="claude",
                model=message.get("model"),
                channel=channel,
                variant=variant,
                service_tier=service_tier,
                context_band=context_band,
                occurred_at=occurred_at,
                measurement_quality=MeasurementQuality.PROVIDER_REPORTED,
                semantics_version="anthropic.v1",
                source=usage,
            ))
    return tuple(records)


def _extract_codex_cost_records(filepath, tz):
    from costs.contracts import MeasurementQuality, Provider

    records = []
    current_model = None
    session_id = None
    previous_total = None
    seen_totals = set()
    with open(filepath, "r", encoding="utf-8") as source_file:
        for line_number, line in enumerate(source_file, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            entry_type = entry.get("type")
            context_type = payload.get("type")
            if entry_type == "session_meta" or context_type == "session_meta":
                native_session_id = payload.get("id") or payload.get("session_id")
                if native_session_id is not None:
                    session_id = str(native_session_id)
            if (
                entry_type in {"session_meta", "turn_context"}
                or context_type in {"session_meta", "turn_context"}
            ):
                candidate = payload.get("model")
                if candidate is not None:
                    current_model = candidate
            if entry_type != "event_msg" or context_type != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            normalized_total = None
            total_snapshot = None
            if isinstance(total, dict):
                normalized_total = normalize_usage(
                    total.get("input_tokens"),
                    total.get("output_tokens"),
                    total.get("cached_input_tokens"),
                    total.get("cache_write_input_tokens"),
                )
                if total:
                    total_snapshot = tuple(
                        normalized_total[field] for field in TOKEN_FIELDS
                    )
            selected = last if isinstance(last, dict) else None
            measurement_quality = MeasurementQuality.PROVIDER_REPORTED
            record_assumptions = ()
            if selected is None and normalized_total is not None:
                total_values = tuple(
                    normalized_total[field] for field in TOKEN_FIELDS
                )
                if previous_total is None:
                    selected_values = total_values
                else:
                    regressed = any(
                        value < previous
                        for value, previous in zip(total_values, previous_total)
                    )
                    if regressed:
                        selected_values = total_values
                        record_assumptions = (
                            CODEX_CUMULATIVE_REGRESSION_ASSUMPTION,
                        )
                        # This snapshot is an explicit reset baseline. Old
                        # cumulative totals must not poison the new sequence.
                        seen_totals.clear()
                    else:
                        selected_values = tuple(
                            value - previous
                            for value, previous in zip(total_values, previous_total)
                        )
                        measurement_quality = MeasurementQuality.DERIVED
                if total_snapshot is not None and total_snapshot in seen_totals:
                    continue
                if total_snapshot is not None:
                    seen_totals.add(total_snapshot)
                previous_total = total_values
                selected = dict(zip((
                    "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"
                ), selected_values))
            elif normalized_total is not None:
                if total_snapshot is not None and total_snapshot in seen_totals:
                    continue
                if total_snapshot is not None:
                    seen_totals.add(total_snapshot)
                previous_total = tuple(
                    normalized_total[field] for field in TOKEN_FIELDS
                )
            occurred_at = parse_timestamp(entry.get("timestamp"), tz)
            if not isinstance(selected, dict) or occurred_at is None:
                continue
            visible_usage = normalize_usage(
                selected.get("input_tokens"),
                selected.get("output_tokens"),
                selected.get("cached_input_tokens"),
                selected.get("cache_write_input_tokens"),
            )
            if not any(visible_usage.values()):
                continue
            model = next(
                (
                    value
                    for value in (
                        selected.get("model"),
                        info.get("model"),
                        payload.get("model"),
                        current_model,
                    )
                    if value is not None
                ),
                None,
            )
            ordinal = entry.get("ordinal", line_number)
            source_id = _bounded_record_id(
                "codex",
                filepath,
                f"{session_id}:{ordinal}" if session_id else ordinal,
                global_identity=session_id is not None,
            )
            records.append(_cost_record(
                Provider.OPENAI.value,
                source_record_id=source_id,
                agent="codex",
                model=model,
                channel="api" if _explicit_api_provenance(entry, payload, info, selected) else "subscription",
                variant=selected.get("variant", "standard"),
                service_tier=selected.get("service_tier", "default"),
                context_band=selected.get("context_band", "standard"),
                occurred_at=occurred_at,
                measurement_quality=measurement_quality,
                semantics_version="openai.inclusive-input.v1",
                assumptions=record_assumptions,
                source={
                    "input_tokens": selected.get("input_tokens"),
                    "cached_input_tokens": selected.get("cached_input_tokens", 0),
                    "cache_write_input_tokens": selected.get("cache_write_input_tokens", 0),
                    "output_tokens": selected.get("output_tokens"),
                    **({"reasoning_output_tokens": selected["reasoning_output_tokens"]} if "reasoning_output_tokens" in selected else {}),
                },
            ))
    return tuple(records)


def _extract_grok_cost_records(filepath, tz):
    from costs.contracts import MeasurementQuality, Provider

    records = []
    with open(filepath, "r", encoding="utf-8") as source_file:
        for line_number, line in enumerate(source_file, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            params = entry.get("params")
            update = params.get("update") if isinstance(params, dict) else None
            usage = update.get("usage") if isinstance(update, dict) else None
            occurred_at = parse_timestamp(
                entry.get("timestamp"),
                tz,
                (params.get("_meta") or {}).get("agentTimestampMs") if isinstance(params, dict) else None,
            )
            if not isinstance(usage, dict) or occurred_at is None:
                continue
            prompt_id = update.get("prompt_id") or f"line-{line_number}"
            session_id = params.get("sessionId") or os.path.dirname(filepath)
            native_api = any(key in usage for key in (
                "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"
            ))
            if native_api:
                response_shape = "input_tokens" in usage or "output_tokens" in usage
                semantics = "xai.responses.v1" if response_shape else "xai.chat-completions.v1"
                raw_source = {"usage": usage}
                channel = "api"
                source_id = _bounded_record_id(
                    "grok",
                    filepath,
                    f"{session_id}:{prompt_id}",
                    global_identity=update.get("prompt_id") is not None,
                )
                model = next((value for value in (
                    usage.get("model"), update.get("model"), params.get("model")
                ) if isinstance(value, str) and value), None)
                records.append(_cost_record(
                    Provider.XAI.value,
                    source_record_id=source_id,
                    agent="grok",
                    model=model,
                    channel=channel,
                    variant=usage.get("variant", "standard"),
                    service_tier=usage.get("service_tier", "default"),
                    context_band=None,
                    occurred_at=occurred_at,
                    measurement_quality=MeasurementQuality.PROVIDER_REPORTED,
                    semantics_version=semantics,
                    source=raw_source,
                ))
                continue

            model_usage = usage.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage:
                usage_records = tuple(model_usage.items())
                split_model_usage = True
            else:
                model = next((value for value in (
                    usage.get("model"), update.get("model"), params.get("model")
                ) if isinstance(value, str) and value), None)
                usage_records = ((model, usage),)
                split_model_usage = False

            for model, model_counters in usage_records:
                exact_model = model if isinstance(model, str) and model else None
                raw_source = model_counters if isinstance(model_counters, dict) else {
                    "malformedModelUsage": model_counters
                }
                source_id = _bounded_record_id(
                    "grok",
                    filepath,
                    (
                        f"{session_id}:{prompt_id}:{exact_model or 'missing-model'}"
                        if split_model_usage
                        else f"{session_id}:{prompt_id}"
                    ),
                    global_identity=update.get("prompt_id") is not None,
                )
                records.append(_cost_record(
                    Provider.XAI.value,
                    source_record_id=source_id,
                    agent="grok",
                    model=exact_model,
                    channel="subscription",
                    variant=usage.get("variant", "standard"),
                    service_tier=usage.get("service_tier", "default"),
                    context_band="standard",
                    occurred_at=occurred_at,
                    measurement_quality=MeasurementQuality.DERIVED,
                    semantics_version="xai.grok-cli.api-equivalent.v1",
                    assumptions=(GROK_CLI_API_EQUIVALENT_ASSUMPTION,),
                    source=raw_source,
                ))
    return tuple(records)


def _extract_composer_cost_records(filepath, tz):
    from costs.contracts import MeasurementQuality, Provider

    records = []
    fallback_timestamp = os.path.getmtime(filepath)
    with open(filepath, "r", encoding="utf-8") as source_file:
        for line_number, line in enumerate(source_file, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = entry.get("role")
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            occurred_at = parse_timestamp(entry.get("timestamp"), tz, fallback_timestamp)
            if occurred_at is None:
                continue
            source_id = _bounded_record_id("composer", filepath, line_number)
            semantics = entry.get("semantics_version")
            if semantics in {"cursor.cloud-agent-run.v1", "cursor.team-usage.v1", "cursor.cloud.flattened.v1"}:
                raw_source = entry.get("source") if isinstance(entry.get("source"), dict) else entry
                quality = MeasurementQuality.PROVIDER_REPORTED
                channel = entry.get("channel")
                model = entry.get("model")
            elif role in {"user", "assistant"}:
                tokens = estimate_tokens(message.get("content"))
                if not tokens:
                    continue
                raw_source = {
                    "estimated_input_tokens" if role == "user" else "estimated_output_tokens": tokens
                }
                semantics = "cursor.local-transcript.v1"
                quality = MeasurementQuality.ESTIMATED
                channel = "local-transcript"
                model = message.get("model") or entry.get("model")
                assumptions = ()
                if not model:
                    model = "composer-2.5"
                    assumptions = (COMPOSER_MODEL_APPROXIMATION_ASSUMPTION,)
            else:
                continue
            records.append(_cost_record(
                Provider.CURSOR.value,
                source_record_id=source_id,
                agent="composer",
                model=model,
                channel=channel,
                variant=(
                    message.get("variant")
                    or entry.get("variant")
                    or ("fast" if channel == "local-transcript" else "standard")
                ),
                service_tier=entry.get("service_tier", "default" if channel == "local-transcript" else "unknown"),
                context_band=entry.get("context_band", "standard"),
                occurred_at=occurred_at,
                measurement_quality=quality,
                semantics_version=semantics,
                assumptions=assumptions if channel == "local-transcript" else (),
                source=raw_source,
            ))
    return tuple(records)


COST_RECORD_EXTRACTORS = {
    "claude": _extract_claude_cost_records,
    "codex": _extract_codex_cost_records,
    "grok": _extract_grok_cost_records,
    "composer": _extract_composer_cost_records,
}


def _plain_cost_json(value):
    if isinstance(value, Mapping):
        return {key: _plain_cost_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_cost_json(item) for item in value]
    return value


def _merge_cost_source_values(current, incoming):
    """Merge same-dimension snapshot source values and report any derivation."""
    if isinstance(current, Mapping) and isinstance(incoming, Mapping):
        merged = {key: _plain_cost_json(value) for key, value in current.items()}
        inconsistent = False
        for key, incoming_value in incoming.items():
            if key not in merged:
                merged[key] = _plain_cost_json(incoming_value)
                inconsistent = True
                continue
            merged_value, changed = _merge_cost_source_values(
                merged[key], incoming_value
            )
            merged[key] = merged_value
            inconsistent = inconsistent or changed
        return merged, inconsistent
    if (
        isinstance(current, int)
        and not isinstance(current, bool)
        and current >= 0
        and isinstance(incoming, int)
        and not isinstance(incoming, bool)
        and incoming >= 0
    ):
        return max(current, incoming), incoming < current
    if current == incoming:
        return _plain_cost_json(current), False
    # Keep a valid visible counter if only the other snapshot is malformed;
    # the unsupported merged semantics still forces whole-record unpricing.
    if isinstance(current, int) and not isinstance(current, bool) and current >= 0:
        return current, True
    if isinstance(incoming, int) and not isinstance(incoming, bool) and incoming >= 0:
        return incoming, True
    return _plain_cost_json(current), True


def _merge_cost_record_group(records):
    if len(records) == 1:
        return records[0]

    from dataclasses import replace

    from costs.contracts import MeasurementQuality

    ordered = sorted(enumerate(records), key=lambda item: (item[1].occurred_at, item[0]))
    base = ordered[0][1]
    merged_source = _plain_cost_json(ordered[0][1].source)
    inconsistent = False
    source_changed = False
    dimensions = (
        "agent",
        "provider",
        "model",
        "channel",
        "variant",
        "service_tier",
        "context_band",
        "measurement_quality",
        "semantics_version",
    )
    expected_dimensions = tuple(getattr(ordered[0][1], name) for name in dimensions)
    assumptions = set()
    for _, record in ordered:
        assumptions.update(record.assumptions)
        if tuple(getattr(record, name) for name in dimensions) != expected_dimensions:
            inconsistent = True
    for _, record in ordered[1:]:
        merged_source, changed = _merge_cost_source_values(
            merged_source, record.source
        )
        source_changed = source_changed or changed
    if inconsistent:
        assumptions.add(MERGE_INCONSISTENCY_ASSUMPTION)
    elif source_changed:
        assumptions.add(STREAMING_MAXIMA_ASSUMPTION)
    return replace(
        base,
        source=merged_source,
        measurement_quality=(
            base.measurement_quality
            if inconsistent or not source_changed
            else MeasurementQuality.DERIVED
        ),
        assumptions=tuple(sorted(assumptions)),
    )


def extract_cost_raw_records(files_by_agent, tz):
    """Read lossless, source-specific records only when costs are enabled."""
    records_by_id = {}
    for agent in AGENT_NAMES:
        extractor = COST_RECORD_EXTRACTORS[agent]
        for filepath in files_by_agent.get(agent, []):
            try:
                extracted = extractor(filepath, tz)
            except CostExtractionError:
                raise
            except (OSError, UnicodeError) as error:
                raise CostExtractionError(
                    f"cost source file could not be read: {filepath}"
                ) from error
            except (AttributeError, OverflowError, TypeError, ValueError) as error:
                raise CostExtractionError(
                    f"cost source record could not be normalized: {filepath}"
                ) from error
            for record in extracted:
                records_by_id.setdefault(record.source_record_id, []).append(record)
    return tuple(
        _merge_cost_record_group(records_by_id[source_id])
        for source_id in sorted(records_by_id)
    )


def calculate_cost_result(raw_records, pricing_pack):
    """Run the central adapter registry and exact-key engine end to end."""
    from dataclasses import replace

    from costs.contracts import Diagnostic, DiagnosticReason, UnpricedQuantity
    from costs.engine import price_quantities
    from costs.registry import ADAPTER_SLOTS

    quantities = []
    unpriced = []
    diagnostics = []
    for record in raw_records:
        adapter = ADAPTER_SLOTS.get(record.provider)
        if adapter is None:
            raise CostExtractionError(
                f"no semantic adapter for visible record {record.source_record_id}"
            )
        normalized = adapter(record)
        inconsistency_assumptions = (
            NORMALIZATION_INCONSISTENCY_ASSUMPTIONS.intersection(record.assumptions)
        )
        if inconsistency_assumptions:
            for quantity in normalized.quantities:
                unpriced.append(
                    UnpricedQuantity(
                        source_record_id=quantity.source_record_id,
                        agent=quantity.agent,
                        provider=quantity.provider.value,
                        model=quantity.model,
                        channel=quantity.channel,
                        variant=quantity.variant,
                        service_tier=quantity.service_tier,
                        context_band=quantity.context_band,
                        occurred_at=quantity.occurred_at,
                        meter=quantity.meter,
                        quantity=quantity.quantity,
                        unit=quantity.unit,
                        measurement_quality=quantity.measurement_quality,
                        semantics_version=quantity.semantics_version,
                        reason=DiagnosticReason.SEMANTICS_INCONSISTENT,
                        assumptions=quantity.assumptions,
                    )
                )
            unpriced.extend(
                replace(
                    quantity,
                    reason=DiagnosticReason.SEMANTICS_INCONSISTENT,
                )
                for quantity in normalized.unpriced_quantities
            )
            if CODEX_CUMULATIVE_REGRESSION_ASSUMPTION in inconsistency_assumptions:
                detail = (
                    "Codex cumulative counters regressed; current provider-normalized "
                    "snapshot forced unpriced and accepted as reset baseline"
                )
            else:
                detail = (
                    "streaming snapshots regressed or changed dimensions; "
                    "provider-normalized maxima forced unpriced"
                )
            diagnostics.append(
                Diagnostic(
                    DiagnosticReason.SEMANTICS_INCONSISTENT,
                    record.source_record_id,
                    detail=detail,
                )
            )
        else:
            quantities.extend(normalized.quantities)
            unpriced.extend(normalized.unpriced_quantities)
        diagnostics.extend(normalized.diagnostics)
    quantities, unpriced = _compact_normalized_costs(quantities, unpriced)
    return price_quantities(
        quantities,
        unpriced,
        pricing_pack,
        diagnostics=diagnostics,
    )


def _compact_normalized_costs(quantities, unpriced, limit=90_000):
    """Bound histories without changing supported quantities into unsupported ones."""
    distinct_assumptions = {
        assumption
        for item in (*quantities, *unpriced)
        for assumption in item.assumptions
    }
    if len(quantities) + len(unpriced) <= limit and len(distinct_assumptions) <= 900:
        return quantities, unpriced

    from costs.contracts import BillableQuantity, DiagnosticReason, UnpricedQuantity

    disclosure = (
        "usage records compacted by exact rate dimensions and hour for bounded serialization"
    )

    billable_groups = {}
    for item in quantities:
        occurred_at = item.occurred_at
        key = (
            item.agent,
            occurred_at.date(),
            occurred_at.hour,
            item.provider,
            item.model,
            item.channel,
            item.variant,
            item.service_tier,
            item.context_band,
            item.meter,
            item.unit,
            item.measurement_quality,
            item.semantics_version,
        )
        if key in billable_groups:
            template, quantity = billable_groups[key]
            if occurred_at < template.occurred_at:
                template = item
            billable_groups[key] = (template, quantity + item.quantity)
        else:
            billable_groups[key] = (item, item.quantity)

    compacted_billable = []
    for key, (template, quantity) in sorted(
        billable_groups.items(), key=lambda pair: repr(pair[0])
    ):
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:24]
        compacted_billable.append(
            BillableQuantity(
                source_record_id=f"cost-compact:billable:{digest}",
                agent=template.agent,
                provider=template.provider,
                model=template.model,
                channel=template.channel,
                variant=template.variant,
                service_tier=template.service_tier,
                context_band=template.context_band,
                occurred_at=template.occurred_at,
                meter=template.meter,
                quantity=quantity,
                unit=template.unit,
                measurement_quality=template.measurement_quality,
                semantics_version=template.semantics_version,
                assumptions=(disclosure,),
            )
        )

    if len(compacted_billable) > limit:
        raise CostExtractionError(
            "history has more exact billable rate/hour scopes than the bounded format can represent"
        )

    def compact_unpriced(level):
        groups = {}
        for item in unpriced:
            occurred_at = item.occurred_at
            provider = getattr(item.provider, "value", item.provider)
            if level == 1:
                grouping_dimensions = (
                    provider,
                    item.model,
                    item.channel,
                    item.variant,
                    item.service_tier,
                    item.context_band,
                    item.meter,
                    item.unit,
                    item.measurement_quality,
                    item.semantics_version,
                    item.reason,
                )
            elif level == 2:
                grouping_dimensions = (
                    provider,
                    item.meter,
                    item.unit,
                    item.measurement_quality,
                    item.semantics_version,
                    item.reason,
                )
            elif level == 3:
                grouping_dimensions = (
                    item.meter,
                    item.unit,
                    item.measurement_quality,
                    item.reason,
                )
            else:
                grouping_dimensions = (item.unit, item.measurement_quality)
            key = (
                item.agent,
                occurred_at.date(),
                occurred_at.hour,
                *grouping_dimensions,
            )
            if key in groups:
                template, quantity = groups[key]
                if occurred_at < template.occurred_at:
                    template = item
                groups[key] = (template, quantity + item.quantity)
            else:
                groups[key] = (item, item.quantity)

        compacted = []
        for key, (template, quantity) in sorted(
            groups.items(), key=lambda pair: repr(pair[0])
        ):
            digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:24]
            provider = getattr(template.provider, "value", template.provider)
            reason = template.reason
            if level >= 2:
                reason = DiagnosticReason.BOUNDED_COMPACTION
            if level >= 3:
                provider = None
            meter = template.meter if level <= 3 else None
            compacted.append(
                UnpricedQuantity(
                    source_record_id=f"cost-compact:unpriced:{digest}",
                    agent=template.agent,
                    provider=provider,
                    model=template.model if level == 1 else None,
                    channel=template.channel if level == 1 else None,
                    variant=template.variant if level == 1 else None,
                    service_tier=template.service_tier if level == 1 else None,
                    context_band=template.context_band if level == 1 else None,
                    occurred_at=template.occurred_at,
                    meter=meter,
                    quantity=quantity,
                    unit=template.unit,
                    measurement_quality=template.measurement_quality,
                    semantics_version=(
                        template.semantics_version
                        if level <= 2
                        else "integration.bounded-compaction.v1"
                    ),
                    reason=reason,
                    assumptions=(disclosure,),
                )
            )
        return compacted

    available = limit - len(compacted_billable)
    if not unpriced:
        return compacted_billable, []
    if available <= 0:
        raise CostExtractionError(
            "history has no bounded component slot for visible unpriced usage"
        )
    for level in (1, 2, 3, 4):
        compacted = compact_unpriced(level)
        if len(compacted) <= available:
            return compacted_billable, compacted
    raise CostExtractionError(
        "history has more hourly cost scopes than the bounded format can represent"
    )


def add_usage(target, usage):
    """Add a normalized usage record into an aggregate."""
    for field in TOKEN_FIELDS:
        target[field] += usage[field]


def totals_from_daily(daily_usage):
    """Calculate canonical totals from a daily usage mapping."""
    totals = {
        field: sum(day[field] for day in daily_usage.values())
        for field in TOKEN_FIELDS
    }
    totals["total_tokens"] = sum(totals.values())
    return totals


CACHE_FORMAT_VERSION = 1


def default_cache_path(search_path, tz):
    """Return the on-disk parse cache path for this search path and timezone."""
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        base = os.path.expanduser(data_home)
    else:
        base = os.path.expanduser("~/.local/share")
    identity = f"{os.path.abspath(os.path.expanduser(search_path))}\0{tz}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, "claude-usage-calendar", f"parse-cache-v1-{key}.json")


def cache_file_identity(agent, filepath):
    """Return a stable basename-level identity for moved-file cache guards."""
    basename = os.path.basename(filepath)
    if agent == "grok":
        parent = os.path.basename(os.path.dirname(filepath))
        return (agent, f"{parent}/{basename}")
    return (agent, basename)


def rename_corrupt_parse_cache(path):
    """Rename an unreadable cache file so history is not silently overwritten."""
    try:
        os.rename(path, f"{path}.corrupt-{int(time.time())}")
    except OSError:
        pass


def parse_cache_script_sha256():
    """Return sha256 hex digest of this script's source bytes."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def record_to_cache_row(record):
    """Serialize a parser record to a compact cache row."""
    usage = record["usage"]
    return [
        record["id"],
        record["date"],
        record["hour"],
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_read_input_tokens"],
        usage["cache_creation_input_tokens"],
    ]


def cache_row_to_record(agent, row):
    """Rebuild a parser record dict from a compact cache row."""
    record_id, date, hour, input_tokens, output_tokens, cache_read, cache_create = row
    return {
        "agent": agent,
        "id": record_id,
        "date": date,
        "hour": hour,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
        },
        "estimated": agent == "composer",
    }


def load_parse_cache(path, tz):
    """Load parse cache entries and whether the script sha256 still matches."""
    if not os.path.isfile(path):
        return {}, True

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        rename_corrupt_parse_cache(path)
        return {}, True

    if not isinstance(payload, dict):
        rename_corrupt_parse_cache(path)
        return {}, True
    if payload.get("format_version") != CACHE_FORMAT_VERSION:
        rename_corrupt_parse_cache(path)
        return {}, True
    if payload.get("tz") != str(tz):
        rename_corrupt_parse_cache(path)
        return {}, True

    sha_matches = payload.get("script_sha256") == parse_cache_script_sha256()

    files = payload.get("files")
    if not isinstance(files, dict):
        rename_corrupt_parse_cache(path)
        return {}, True

    entries = {}
    for filepath, entry in files.items():
        if not isinstance(filepath, str) or not isinstance(entry, dict):
            rename_corrupt_parse_cache(path)
            return {}, True
        agent = entry.get("agent")
        if agent not in AGENT_NAMES:
            rename_corrupt_parse_cache(path)
            return {}, True
        for key in ("st_size", "st_mtime_ns"):
            if not isinstance(entry.get(key), int):
                rename_corrupt_parse_cache(path)
                return {}, True
        rows = entry.get("rows")
        if not isinstance(rows, list):
            rename_corrupt_parse_cache(path)
            return {}, True
        for row in rows:
            if not isinstance(row, list) or len(row) != 7:
                rename_corrupt_parse_cache(path)
                return {}, True
            if not all(isinstance(value, int) for value in row[2:]):
                rename_corrupt_parse_cache(path)
                return {}, True
            if not isinstance(row[0], str) or not isinstance(row[1], str):
                rename_corrupt_parse_cache(path)
                return {}, True
        entries[filepath] = {
            "agent": agent,
            "st_size": entry["st_size"],
            "st_mtime_ns": entry["st_mtime_ns"],
            "rows": rows,
        }
    return entries, sha_matches


def save_parse_cache(path, tz, entries):
    """Atomically write the parse cache for the current file set."""
    payload = {
        "format_version": CACHE_FORMAT_VERSION,
        "script_sha256": parse_cache_script_sha256(),
        "tz": str(tz),
        "files": entries,
    }
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        fd, temp_path = tempfile.mkstemp(
            prefix=".parse-cache-", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            os.chmod(path, 0o600)
        except OSError:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except OSError:
        return


def parse_session_files(files_by_agent, tz, cache_path=None, stats=None):
    """Parse all supported formats into combined and per-agent aggregates."""
    deduplicated_records = {}
    file_counts = {agent: len(files_by_agent.get(agent, [])) for agent in AGENT_NAMES}
    daily_usage = defaultdict(empty_usage)
    hourly_usage = defaultdict(lambda: defaultdict(empty_usage))
    agent_daily = {agent: defaultdict(empty_usage) for agent in AGENT_NAMES}
    agent_hourly = {
        agent: defaultdict(lambda: defaultdict(empty_usage)) for agent in AGENT_NAMES
    }
    agent_messages = {agent: 0 for agent in AGENT_NAMES}

    def aggregate(record):
        agent = record["agent"]
        add_usage(daily_usage[record["date"]], record["usage"])
        add_usage(hourly_usage[record["date"]][record["hour"]], record["usage"])
        add_usage(agent_daily[agent][record["date"]], record["usage"])
        add_usage(
            agent_hourly[agent][record["date"]][record["hour"]], record["usage"]
        )
        agent_messages[agent] += 1

    def ingest_record(record):
        agent = record["agent"]
        if agent in ("codex", "composer"):
            aggregate(record)
            return

        existing = deduplicated_records.get(record["id"])
        if existing is None:
            deduplicated_records[record["id"]] = record
        else:
            for field in TOKEN_FIELDS:
                existing["usage"][field] = max(
                    existing["usage"][field], record["usage"][field]
                )

    cached_entries = {}
    sha_matches = True
    if cache_path:
        cached_entries, sha_matches = load_parse_cache(cache_path, tz)

    current_identities = set()
    for agent in AGENT_NAMES:
        for filepath in files_by_agent.get(agent, []):
            current_identities.add(cache_file_identity(agent, filepath))

    cache_writes = {}
    listed_paths = set()
    historical_files = 0

    for agent in AGENT_NAMES:
        parser = PARSERS[agent]
        for filepath in files_by_agent.get(agent, []):
            abs_path = os.path.abspath(filepath)
            listed_paths.add(abs_path)
            cached = cached_entries.get(abs_path)
            try:
                stat_result = os.stat(filepath)
            except OSError:
                if cached is not None:
                    for row in cached["rows"]:
                        ingest_record(cache_row_to_record(cached["agent"], row))
                    if cache_path is not None:
                        cache_writes[abs_path] = cached
                continue

            file_meta = {
                "agent": agent,
                "st_size": stat_result.st_size,
                "st_mtime_ns": stat_result.st_mtime_ns,
            }
            file_rows = None
            if (
                sha_matches
                and cached is not None
                and cached["agent"] == agent
                and cached["st_size"] == file_meta["st_size"]
                and cached["st_mtime_ns"] == file_meta["st_mtime_ns"]
            ):
                file_rows = cached["rows"]

            if file_rows is None:
                file_rows = []
                try:
                    for record in parser(filepath, tz):
                        file_rows.append(record_to_cache_row(record))
                        ingest_record(record)
                except OSError:
                    if cached is not None and cache_path is not None:
                        cache_writes[abs_path] = cached
                    continue
                except (UnicodeError, AttributeError, TypeError, ValueError):
                    pass
            else:
                for row in file_rows:
                    ingest_record(cache_row_to_record(agent, row))

            if cache_path is not None and abs_path not in cache_writes:
                cache_writes[abs_path] = {**file_meta, "rows": file_rows}

    if cache_path is not None:
        for path, entry in cached_entries.items():
            if path in listed_paths or path in cache_writes:
                continue
            if os.path.exists(path):
                continue
            if cache_file_identity(entry["agent"], path) in current_identities:
                continue
            for row in entry["rows"]:
                ingest_record(cache_row_to_record(entry["agent"], row))
            cache_writes[path] = entry
            historical_files += 1

    if stats is not None:
        stats["historical_files"] = historical_files

    if cache_path is not None:
        save_parse_cache(cache_path, tz, cache_writes)

    for record in deduplicated_records.values():
        aggregate(record)

    hourly_dict = {}
    for date_key, hours in hourly_usage.items():
        hourly_dict[date_key] = {str(h): dict(v) for h, v in hours.items()}

    agents = {}
    for agent in AGENT_NAMES:
        daily = {date: dict(usage) for date, usage in agent_daily[agent].items()}
        hourly = {
            date: {str(hour): dict(usage) for hour, usage in hours.items()}
            for date, hours in agent_hourly[agent].items()
        }
        agents[agent] = {
            "display_name": AGENT_DISPLAY_NAMES[agent],
            "color": AGENT_COLORS[agent],
            "files": file_counts[agent],
            "unique_messages": agent_messages[agent],
            "estimated": agent == "composer" and file_counts[agent] > 0,
            "totals": totals_from_daily(daily),
            "daily_usage": daily,
            "hourly_usage": hourly,
        }

    return dict(daily_usage), hourly_dict, sum(agent_messages.values()), agents


def parse_jsonl_files(files, tz):
    """Backward-compatible Claude parser entry point."""
    files_by_agent = {agent: [] for agent in AGENT_NAMES}
    files_by_agent["claude"] = files
    daily, hourly, count, _agents = parse_session_files(files_by_agent, tz)
    return daily, hourly, count


def format_tokens(n):
    """Format token count as K/M/B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    elif n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_usage_data(daily_usage, hourly_usage, msg_count, tz_label, agents=None):
    """Build the canonical JSON data structure from parsed usage data."""
    dates = sorted(daily_usage.keys())
    totals = totals_from_daily(daily_usage)

    return {
        "timezone": tz_label,
        "date_range": {
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
        },
        "days_with_data": len(daily_usage),
        "unique_messages": msg_count,
        "totals": totals,
        "agents": agents or {},
        "daily_usage": daily_usage,
        "hourly_usage": hourly_usage,
    }


def generate_html(usage_data, cost_result=None):
    """Generate interactive HTML with all views."""
    # Extract data from the canonical structure
    daily_usage = usage_data["daily_usage"]
    hourly_usage = usage_data["hourly_usage"]
    agents = usage_data.get("agents", {})
    tz_label = usage_data["timezone"]
    date_range = usage_data["date_range"]

    # Convert daily_usage and hourly_usage to JSON for embedding
    daily_data_json = json.dumps(daily_usage)
    hourly_data_json = json.dumps(hourly_usage)
    agent_data_json = json.dumps(agents)
    cost_estimates = usage_data.get("cost_estimates")
    cost_data_declaration = "const costData = null;"
    cost_dashboard_html = ""
    if cost_estimates is not None:
        if cost_result is None:
            raise ValueError("cost_result is required when cost_estimates are present")
        from costs.dashboard import render_cost_dashboard

        cost_data_json = (
            json.dumps(cost_estimates, ensure_ascii=False, separators=(",", ":"))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        cost_data_declaration = f"const costData = {cost_data_json};"
        fragment = render_cost_dashboard(cost_result, cost_estimates).fragment.html
        cost_dashboard_html = (
            '<div id="cost-dashboard-container" hidden>' + fragment + "</div>"
        )

    # Find date range
    if date_range["start"]:
        min_date = date_range["start"]
        max_date = date_range["end"]
        min_year = int(min_date[:4])
        max_year = int(max_date[:4])
    else:
        now = datetime.now().astimezone()
        min_year = max_year = now.year
        min_date = max_date = now.strftime("%Y-%m-%d")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Agent Token Usage</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        html {{
            background: #0a0818;
        }}

        body {{
            font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #1a1a3e 50%, #24243e 100%);
            min-height: 100vh;
            padding: 20px;
            color: #e0e0e0;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        .header-bar {{
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            align-items: center;
            margin-bottom: 15px;
        }}

        .header-bar .tz-label {{
            text-align: right;
        }}

        h1 {{
            font-size: 1.6rem;
            font-weight: 700;
            background: linear-gradient(90deg, #00d4ff, #00ff88, #00d4ff);
            background-size: 200% auto;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            animation: gradient 3s linear infinite;
            margin: 0;
        }}

        .tz-label {{
            color: #666;
            font-size: 0.8rem;
        }}

        @keyframes gradient {{
            0% {{ background-position: 0% center; }}
            100% {{ background-position: 200% center; }}
        }}

        /* Navigation Tabs */
        .nav-tabs {{
            display: flex;
            justify-content: center;
            gap: 8px;
        }}

        .nav-tab {{
            padding: 8px 18px;
            background: rgba(40, 40, 80, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 6px;
            color: #888;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 500;
            transition: all 0.3s ease;
        }}

        .nav-tab:hover {{
            background: rgba(60, 60, 100, 0.6);
            color: #fff;
            border-color: rgba(0, 212, 255, 0.3);
        }}

        .nav-tab.active {{
            background: linear-gradient(135deg, rgba(0, 212, 255, 0.3) 0%, rgba(0, 255, 136, 0.2) 100%);
            border-color: rgba(0, 212, 255, 0.5);
            color: #00d4ff;
        }}

        .breakdown-toggle {{
            display: flex;
            justify-content: center;
            gap: 4px;
            width: fit-content;
            margin: 0 auto 15px;
            padding: 4px;
            border-radius: 9px;
            background: rgba(20, 20, 45, 0.75);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }}

        .breakdown-option {{
            appearance: none;
            border: 1px solid transparent;
            border-radius: 6px;
            padding: 7px 16px;
            background: transparent;
            color: #777;
            cursor: pointer;
            font: inherit;
            font-size: 0.8rem;
            font-weight: 600;
            transition: all 0.2s ease;
        }}

        .breakdown-option:hover {{
            color: #ddd;
        }}

        .breakdown-option.active {{
            color: #fff;
            background: rgba(0, 212, 255, 0.18);
            border-color: rgba(0, 212, 255, 0.35);
            box-shadow: 0 2px 12px rgba(0, 212, 255, 0.12);
        }}

        /* Sub Navigation */
        .sub-nav {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 15px;
            margin-bottom: 15px;
        }}

        .nav-arrow {{
            padding: 5px 12px;
            background: rgba(40, 40, 80, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 5px;
            color: #888;
            cursor: pointer;
            font-size: 1rem;
            transition: all 0.3s ease;
        }}

        .nav-arrow:hover:not(.disabled) {{
            background: rgba(60, 60, 100, 0.6);
            color: #00d4ff;
            border-color: rgba(0, 212, 255, 0.3);
        }}

        .nav-arrow.disabled {{
            opacity: 0.3;
            cursor: not-allowed;
        }}

        .nav-current {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            min-width: 160px;
            text-align: center;
        }}

        /* Content area */
        .view-content {{
            display: none;
        }}

        .view-content.active {{
            display: block;
        }}

        .calendar {{
            background: rgba(30, 30, 60, 0.6);
            border-radius: 20px;
            padding: 30px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
        }}

        .calendar-header {{
            display: grid;
            grid-template-columns: repeat(7, 1fr) 120px;
            gap: 10px;
            margin-bottom: 15px;
        }}

        .header-cell {{
            text-align: center;
            font-weight: 600;
            font-size: 0.9rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            padding: 10px;
        }}

        .week-row {{
            display: grid;
            grid-template-columns: repeat(7, 1fr) 120px;
            gap: 10px;
            margin-bottom: 10px;
        }}

        .day-cell {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 8px;
            padding: 6px 8px;
            min-height: 58px;
            transition: all 0.3s ease;
            border: 1px solid rgba(255, 255, 255, 0.05);
            position: relative;
            overflow: hidden;
        }}

        .day-cell:hover {{
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(0, 212, 255, 0.2);
            border-color: rgba(0, 212, 255, 0.3);
        }}

        .day-cell.clickable {{
            cursor: pointer;
        }}

        .day-cell.empty {{
            background: rgba(20, 20, 40, 0.3);
            border: none;
        }}

        .day-cell.empty:hover {{
            transform: none;
            box-shadow: none;
        }}

        .day-cell.other-month {{
            opacity: 0.7;
        }}

        .day-cell.other-month .day-total,
        .day-cell.other-month .day-breakdown span {{
            color: #777 !important;
        }}

        .day-cell.other-month .day-number {{
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.1);
            color: #777;
        }}

        /* Keyboard shortcuts modal */
        .modal-overlay {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }}

        .modal-overlay.active {{
            display: flex;
        }}

        .modal {{
            background: linear-gradient(135deg, #1a1a3e 0%, #24243e 100%);
            border: 1px solid rgba(0, 212, 255, 0.3);
            border-radius: 16px;
            padding: 30px;
            max-width: 400px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
        }}

        .modal h3 {{
            color: #00d4ff;
            font-size: 1.1rem;
            margin-bottom: 5px;
            text-align: center;
        }}

        .modal-hint {{
            text-align: center;
            color: #666;
            font-size: 0.8rem;
            margin-bottom: 15px;
        }}

        kbd {{
            background: rgba(0, 212, 255, 0.15);
            border: 1px solid rgba(0, 212, 255, 0.4);
            border-radius: 4px;
            padding: 3px 8px;
            font-family: monospace;
            font-size: 0.85rem;
            color: #00d4ff;
            display: inline-block;
            min-width: 24px;
            text-align: center;
        }}

        .modal-divider {{
            height: 1px;
            background: rgba(255, 255, 255, 0.1);
            margin: 15px 0;
        }}

        .shortcut-table {{
            width: 100%;
            border-collapse: collapse;
        }}

        .shortcut-table tr {{
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .shortcut-table tr:last-child {{
            border-bottom: none;
        }}

        .shortcut-table td {{
            padding: 8px 0;
            color: #aaa;
            font-size: 0.9rem;
        }}

        .shortcut-table td:first-child {{
            width: 90px;
            padding-right: 15px;
        }}

        .protip {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #1a1a3e;
            border: 1px solid rgba(0, 212, 255, 0.3);
            border-radius: 8px;
            padding: 8px 14px;
            font-size: 0.8rem;
            color: #888;
            z-index: 100;
            transition: opacity 0.3s ease;
        }}

        .protip kbd {{
            background: rgba(0, 212, 255, 0.2);
            border: 1px solid rgba(0, 212, 255, 0.4);
            border-radius: 4px;
            padding: 2px 6px;
            font-family: monospace;
            color: #00d4ff;
        }}

        .protip.hidden {{
            opacity: 0;
            pointer-events: none;
        }}

        .github-link {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            color: #666;
            text-decoration: none;
            font-size: 0.8rem;
            transition: color 0.2s ease;
            margin-top: 15px;
        }}

        .github-link:hover {{
            color: #00d4ff;
        }}

        .github-link svg {{
            width: 18px;
            height: 18px;
            fill: currentColor;
        }}

        .day-header {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 3px;
        }}

        .day-total {{
            font-size: 1rem;
            font-weight: 700;
            color: #00d4ff;
        }}

        .day-number {{
            font-size: 0.75rem;
            font-weight: 600;
            color: #fff;
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 4px;
            padding: 2px 6px;
            min-width: 22px;
            text-align: center;
        }}

        .day-breakdown {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1px 6px;
            font-size: 0.6rem;
            color: #888;
            line-height: 1.2;
        }}

        .in-label {{ color: #4ade80; }}
        .out-label {{ color: #f472b6; }}
        .cache-r-label {{ color: #fbbf24; }}
        .cache-c-label {{ color: #a78bfa; }}

        .week-total {{
            background: rgba(0, 212, 255, 0.1);
            border-radius: 12px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            border: 1px solid rgba(0, 212, 255, 0.2);
        }}

        .week-total-label {{
            font-size: 0.7rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 5px;
        }}

        .week-total-value {{
            font-size: 1.2rem;
            font-weight: 700;
            color: #00d4ff;
        }}

        .summary {{
            margin-top: 20px;
            background: linear-gradient(135deg, rgba(0, 212, 255, 0.1) 0%, rgba(0, 255, 136, 0.1) 100%);
            border-radius: 12px;
            padding: 15px 20px;
            border: 1px solid rgba(0, 212, 255, 0.2);
        }}

        .summary h2 {{
            font-size: 1.2rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 20px;
            text-align: center;
        }}

        .summary-row {{
            display: flex;
            justify-content: flex-start;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .summary-title {{
            font-size: 0.85rem;
            font-weight: 600;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-right: 8px;
        }}

        .summary-inline {{
            display: flex;
            align-items: baseline;
            gap: 8px;
            background: rgba(0, 0, 0, 0.25);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 8px 14px;
        }}

        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 20px;
        }}

        .summary-item {{
            text-align: center;
            padding: 15px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 12px;
        }}

        .summary-label {{
            font-size: 0.75rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }}

        .summary-label-inline {{
            font-size: 0.8rem;
            color: #888;
        }}

        .summary-value {{
            font-size: 1.5rem;
            font-weight: 700;
        }}

        .summary-value.input {{ color: #4ade80; }}
        .summary-value.output {{ color: #f472b6; }}
        .summary-value.cache-read {{ color: #fbbf24; }}
        .summary-value.cache-create {{ color: #a78bfa; }}
        .summary-value.total {{
            background: linear-gradient(90deg, #00d4ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .intensity-low {{ background: rgba(40, 40, 80, 0.5); }}
        .intensity-1 {{ background: linear-gradient(135deg, rgba(0, 80, 100, 0.6) 0%, rgba(40, 40, 80, 0.5) 100%); }}
        .intensity-2 {{ background: linear-gradient(135deg, rgba(0, 120, 130, 0.6) 0%, rgba(40, 60, 90, 0.5) 100%); }}
        .intensity-3 {{ background: linear-gradient(135deg, rgba(0, 160, 160, 0.7) 0%, rgba(40, 80, 100, 0.5) 100%); }}
        .intensity-4 {{ background: linear-gradient(135deg, rgba(0, 200, 180, 0.7) 0%, rgba(40, 100, 110, 0.5) 100%); }}
        .intensity-5 {{ background: linear-gradient(135deg, rgba(0, 212, 255, 0.8) 0%, rgba(0, 180, 150, 0.6) 100%); }}

        /* Yearly View */
        .year-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 30px;
        }}

        .month-card {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 16px;
            padding: 20px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            cursor: pointer;
            transition: all 0.3s ease;
        }}

        .month-card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(0, 212, 255, 0.25);
            border-color: rgba(0, 212, 255, 0.4);
        }}

        .month-card.no-data {{
            opacity: 0.4;
            cursor: default;
        }}

        .month-card.no-data:hover {{
            transform: none;
            box-shadow: none;
            border-color: rgba(255, 255, 255, 0.05);
        }}

        .month-name {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 12px;
        }}

        .month-total {{
            font-size: 1.8rem;
            font-weight: 700;
            color: #00d4ff;
            margin-bottom: 10px;
        }}

        .month-breakdown {{
            font-size: 0.75rem;
            color: #888;
            line-height: 1.6;
        }}

        /* All Time View */
        .all-time-header {{
            text-align: center;
            margin-bottom: 25px;
        }}

        .all-time-date-range {{
            font-size: 1.2rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 5px;
        }}

        .all-time-days-count {{
            font-size: 0.9rem;
            color: #888;
        }}

        .all-time-stats {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }}

        .all-time-breakdown {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }}

        .stat-card {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 16px;
            padding: 25px;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .stat-label {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }}

        .stat-value {{
            font-size: 2rem;
            font-weight: 700;
        }}

        .stat-value.highlight {{
            background: linear-gradient(90deg, #00d4ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .token-breakdown {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
        }}

        .token-card {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 16px;
            padding: 25px;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .token-card-label {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }}

        .token-card-value {{
            font-size: 2.2rem;
            font-weight: 700;
        }}

        .token-card-pct {{
            font-size: 0.9rem;
            color: #666;
            margin-top: 8px;
        }}

        /* Daily View */
        .daily-header {{
            text-align: center;
            margin-bottom: 25px;
        }}

        .daily-date {{
            font-size: 1.4rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 5px;
        }}

        .daily-total {{
            font-size: 2.2rem;
            font-weight: 700;
            background: linear-gradient(90deg, #00d4ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .hourly-chart-wrapper {{
            display: flex;
            background: rgba(20, 20, 40, 0.4);
            border-radius: 12px;
            margin-bottom: 20px;
            padding: 20px 15px 10px 10px;
        }}

        .y-axis {{
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            align-items: flex-end;
            padding-right: 10px;
            padding-bottom: 28px;
            height: 260px;
        }}

        .y-axis-label {{
            font-size: 0.7rem;
            color: #666;
            font-family: monospace;
            line-height: 1;
        }}

        .hourly-chart {{
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            height: 260px;
            flex: 1;
            gap: 4px;
            border-left: 1px solid rgba(255, 255, 255, 0.1);
            padding-left: 10px;
        }}

        .hour-bar-container {{
            display: flex;
            flex-direction: column;
            align-items: center;
            flex: 1;
            height: 100%;
        }}

        .hour-bar-wrapper {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-end;
            flex: 1;
            width: 100%;
        }}

        .hour-bar {{
            width: 100%;
            max-width: 40px;
            min-height: 2px;
            border-radius: 4px 4px 0 0;
            transition: all 0.3s ease;
            cursor: pointer;
            position: relative;
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
        }}

        .hour-bar:hover {{
            filter: brightness(1.2);
            transform: scaleX(1.1);
        }}

        .hour-bar .bar-segment {{
            width: 100%;
            transition: all 0.3s ease;
        }}

        .hour-bar .bar-segment.input {{
            background: #4ade80;
            border-radius: 0;
        }}

        .hour-bar .bar-segment.output {{
            background: #f472b6;
        }}

        .hour-bar .bar-segment.cache-read {{
            background: #fbbf24;
        }}

        .hour-bar .bar-segment.cache-create {{
            background: #a78bfa;
            border-radius: 4px 4px 0 0;
        }}

        .hour-bar .bar-segment:first-child {{
            border-radius: 0 0 4px 4px;
        }}

        .hour-bar .bar-segment:last-child {{
            border-radius: 4px 4px 0 0;
        }}

        .hour-bar .bar-segment:only-child {{
            border-radius: 4px;
        }}

        .hour-label {{
            font-size: 0.7rem;
            color: #666;
            margin-top: 8px;
            text-align: center;
        }}

        .hour-bar-tooltip {{
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(20, 20, 40, 0.95);
            border: 1px solid rgba(0, 212, 255, 0.3);
            border-radius: 8px;
            padding: 10px 14px;
            white-space: nowrap;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s ease;
            z-index: 100;
            margin-bottom: 8px;
        }}

        .hour-bar:hover .hour-bar-tooltip {{
            opacity: 1;
        }}

        .tooltip-hour {{
            font-size: 0.85rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 6px;
        }}

        .tooltip-total {{
            font-size: 1.1rem;
            font-weight: 700;
            color: #00d4ff;
            margin-bottom: 6px;
        }}

        .tooltip-breakdown {{
            font-size: 0.75rem;
            line-height: 1.5;
        }}

        .tooltip-breakdown .in-label {{ color: #4ade80; }}
        .tooltip-breakdown .out-label {{ color: #f472b6; }}
        .tooltip-breakdown .cache-r-label {{ color: #fbbf24; }}
        .tooltip-breakdown .cache-c-label {{ color: #a78bfa; }}

        .chart-legend {{
            display: flex;
            justify-content: center;
            gap: 20px;
            margin-top: 15px;
            flex-wrap: wrap;
        }}

        .legend-item {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.8rem;
            color: #888;
        }}

        .legend-color {{
            width: 12px;
            height: 12px;
            border-radius: 3px;
        }}

        .legend-color.input {{ background: #4ade80; }}
        .legend-color.output {{ background: #f472b6; }}
        .legend-color.cache-read {{ background: #fbbf24; }}
        .legend-color.cache-create {{ background: #a78bfa; }}

        .provider-claude {{ color: #d97757; }}
        .provider-codex {{ color: #10a37f; }}
        .provider-grok {{ color: #4da3ff; }}
        .provider-composer {{ color: #a855f7; }}

        .legend-color.provider-claude,
        .bar-segment.provider-claude {{ background: #d97757; }}
        .legend-color.provider-codex,
        .bar-segment.provider-codex {{ background: #10a37f; }}
        .legend-color.provider-grok,
        .bar-segment.provider-grok {{ background: #4da3ff; }}
        .legend-color.provider-composer,
        .bar-segment.provider-composer {{ background: #a855f7; }}

        .summary-item.provider-claude {{ border-top: 2px solid #d97757; }}
        .summary-item.provider-codex {{ border-top: 2px solid #10a37f; }}
        .summary-item.provider-grok {{ border-top: 2px solid #4da3ff; }}
        .summary-item.provider-composer {{ border-top: 2px solid #a855f7; }}
    </style>
</head>
<body>
    <div class="protip" id="protip">protip: press <kbd>?</kbd> for keyboard shortcuts</div>
    <div class="container">
        <div class="header-bar">
            <h1>AI Agent Token Usage</h1>
            <div class="nav-tabs">
                <div class="nav-tab active" data-view="daily">📊 Daily</div>
                <div class="nav-tab" data-view="monthly">📆 Monthly</div>
                <div class="nav-tab" data-view="yearly">📅 Yearly</div>
                <div class="nav-tab" data-view="alltime">🔢 All Time</div>
            </div>
            <div class="tz-label">{tz_label}</div>
        </div>

        <div class="breakdown-toggle" role="group" aria-label="Usage breakdown">
            <button class="breakdown-option active" type="button" data-breakdown="tokens">Token Types</button>
            <button class="breakdown-option" type="button" data-breakdown="providers">Providers</button>
        </div>

        <div class="sub-nav" id="sub-nav" style="display: none;">
            <div class="nav-arrow" id="nav-prev">◀</div>
            <div class="nav-current" id="nav-current"></div>
            <div class="nav-arrow" id="nav-next">▶</div>
        </div>

        <div class="view-content active" id="view-daily">
            <div class="calendar">
                <div class="daily-header" id="daily-header"></div>
                <div class="hourly-chart-wrapper">
                    <div class="y-axis" id="y-axis"></div>
                    <div class="hourly-chart" id="hourly-chart"></div>
                </div>
                <div class="summary" id="daily-summary"></div>
            </div>
        </div>

        <div class="view-content" id="view-alltime">
            <div class="calendar">
                <div class="all-time-header" id="all-time-header"></div>
                <div class="all-time-stats" id="all-time-stats"></div>
                <div class="summary" id="all-time-summary"></div>
            </div>
        </div>

        <div class="view-content" id="view-yearly">
            <div class="calendar">
                <div class="year-grid" id="year-grid"></div>
                <div class="summary" id="year-summary"></div>
            </div>
        </div>

        <div class="view-content" id="view-monthly">
            <div class="calendar" id="monthly-calendar"></div>
        </div>
        {cost_dashboard_html}
    </div>

    <div class="modal-overlay" id="help-modal">
        <div class="modal">
            <h3>Keyboard Shortcuts</h3>
            <div class="modal-hint">press esc to close</div>
            <div class="modal-divider"></div>
            <table class="shortcut-table">
                <tr><td><kbd>1</kbd></td><td>Daily view</td></tr>
                <tr><td><kbd>2</kbd></td><td>Monthly view</td></tr>
                <tr><td><kbd>3</kbd></td><td>Yearly view</td></tr>
                <tr><td><kbd>4</kbd></td><td>All Time view</td></tr>
                <tr><td><kbd>b</kbd></td><td>Toggle token/provider breakdown</td></tr>
                <tr><td><kbd>h</kbd> / <kbd>k</kbd></td><td>Previous (day/month/year)</td></tr>
                <tr><td><kbd>j</kbd> / <kbd>l</kbd></td><td>Next (day/month/year)</td></tr>
                <tr><td><kbd>t</kbd></td><td>Today (latest day with data)</td></tr>
                <tr><td><kbd>?</kbd></td><td>Show this help</td></tr>
            </table>
            <div class="modal-divider"></div>
            <a href="https://github.com/rickgorman/claude-usage-calendar" target="_blank" class="github-link">
                <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
                rickgorman/claude-usage-calendar
            </a>
        </div>
    </div>

    <script>
        const dailyData = {daily_data_json};
        const hourlyData = {hourly_data_json};
        const agentData = {agent_data_json};
        {cost_data_declaration}
        const minYear = {min_year};
        const maxYear = {max_year};
        const minDate = '{min_date}';
        const maxDate = '{max_date}';
        const monthNames = ['January', 'February', 'March', 'April', 'May', 'June',
                           'July', 'August', 'September', 'October', 'November', 'December'];
        const dayNames = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
        const providerKeys = ['claude', 'codex', 'grok', 'composer'];

        let currentView = 'daily';
        let breakdownMode = 'tokens';
        let currentYear = maxYear;
        let currentMonth = new Date().getMonth() + 1;
        let currentDate = maxDate;

        // Find latest date/month with data
        const dates = Object.keys(dailyData).sort();
        if (dates.length > 0) {{
            const latestDate = dates[dates.length - 1];
            currentDate = latestDate;
            currentYear = parseInt(latestDate.substring(0, 4));
            currentMonth = parseInt(latestDate.substring(5, 7));
        }}

        function formatTokens(n) {{
            if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
            if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
            if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
            return n.toString();
        }}

        function getTotal(usage) {{
            return (usage.input_tokens || 0) + (usage.output_tokens || 0) +
                   (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0);
        }}

        function getTotalForSummary(usage) {{
            // Only count input and output tokens, not cache tokens
            return (usage.input_tokens || 0) + (usage.output_tokens || 0);
        }}

        function emptyTokenUsage() {{
            return {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
        }}

        function providerLabel(key, compact = false) {{
            const agent = agentData[key] || {{}};
            const estimate = agent.estimated ? (compact ? '*' : ' · estimated') : '';
            return `${{agent.display_name || key}}${{estimate}}`;
        }}

        function providerValuesForDate(date, hour = null) {{
            const values = {{}};
            for (const key of providerKeys) {{
                const agent = agentData[key] || {{}};
                const usage = hour === null
                    ? (agent.daily_usage || {{}})[date]
                    : (((agent.hourly_usage || {{}})[date] || {{}})[String(hour)]);
                values[key] = getTotal(usage || {{}});
            }}
            return values;
        }}

        function aggregateProviderValues(predicate) {{
            const values = Object.fromEntries(providerKeys.map(key => [key, 0]));
            for (const key of providerKeys) {{
                const daily = (agentData[key] || {{}}).daily_usage || {{}};
                for (const [date, usage] of Object.entries(daily)) {{
                    if (predicate(date)) values[key] += getTotal(usage);
                }}
            }}
            return values;
        }}

        function addProviderValues(target, source) {{
            for (const key of providerKeys) target[key] += source[key] || 0;
        }}

        function sumProviderValues(values) {{
            return providerKeys.reduce((sum, key) => sum + (values[key] || 0), 0);
        }}

        function providerBreakdownHtml(values, lineBreaks = false, compact = false) {{
            const separator = lineBreaks ? '<br>' : '';
            return providerKeys.map(key =>
                `<span class="provider-${{key}}">${{providerLabel(key, compact)}}: ${{formatTokens(values[key] || 0)}}</span>`
            ).join(separator);
        }}

        function costRollup(agentKey, scope, key, hour = null) {{
            if (!costData) return null;
            const owner = agentKey === null
                ? costData
                : ((costData.agents || {{}})[agentKey] || null);
            if (!owner) return null;
            if (scope === 'total') return owner;
            const collection = owner[`${{scope}}_usage`] || {{}};
            if (scope === 'hourly') return (collection[key] || {{}})[String(hour)] || null;
            return collection[key] || null;
        }}

        function formatBackendUsd(amount) {{
            if (!/^(0|[1-9][0-9]*)\\.[0-9]{{6}}$/.test(amount || '')) return 'Unavailable';
            const [whole, fraction] = amount.split('.');
            let cents = (whole + fraction.slice(0, 2)).split('');
            if (fraction[2] >= '5') {{
                let index = cents.length - 1;
                while (index >= 0 && cents[index] === '9') {{
                    cents[index] = '0';
                    index -= 1;
                }}
                if (index < 0) cents.unshift('1');
                else cents[index] = String(Number(cents[index]) + 1);
            }}
            const digits = cents.join('').padStart(3, '0');
            return `$${{digits.slice(0, -2)}}.${{digits.slice(-2)}}`;
        }}

        function formatBackendCost(rollup) {{
            if (!rollup) return '';
            const complete = rollup.status === 'complete';
            const amount = complete ? rollup.total_usd : rollup.priced_subtotal_usd;
            const label = complete ? 'Estimated cost' : 'Priced subtotal';
            return `${{label}}: ${{formatBackendUsd(amount)}} USD · ${{rollup.status}}`;
        }}

        function providerCostBreakdownHtml(scope, key, hour = null, lineBreaks = true) {{
            if (!costData) return '';
            const separator = lineBreaks ? '<br>' : ' · ';
            return providerKeys.map(agentKey => {{
                const formatted = formatBackendCost(costRollup(agentKey, scope, key, hour));
                return formatted
                    ? `<span class="provider-${{agentKey}}">${{providerLabel(agentKey, true)}} ${{formatted}}</span>`
                    : '';
            }}).filter(Boolean).join(separator);
        }}

        function dateBreakdownHtml(date, usage) {{
            return breakdownMode === 'providers'
                ? providerBreakdownHtml(providerValuesForDate(date), false, true) +
                  (costData ? `<br>${{providerCostBreakdownHtml('daily', date, null, false)}}` : '')
                : `
                    <span class="in-label">In: ${{formatTokens(usage.input_tokens || 0)}}</span>
                    <span class="out-label">Out: ${{formatTokens(usage.output_tokens || 0)}}</span>
                    <span class="cache-r-label">CR: ${{formatTokens(usage.cache_read_input_tokens || 0)}}</span>
                    <span class="cache-c-label">CC: ${{formatTokens(usage.cache_creation_input_tokens || 0)}}</span>
                `;
        }}

        function providerLegendHtml() {{
            return providerKeys.map(key => `
                <div class="legend-item">
                    <div class="legend-color provider-${{key}}"></div>${{providerLabel(key)}}
                </div>
            `).join('');
        }}

        function providerBarSegments(values, total) {{
            if (!total) return '';
            return [...providerKeys].reverse().map(key => {{
                const pct = ((values[key] || 0) / total) * 100;
                return pct > 0
                    ? `<div class="bar-segment provider-${{key}}" style="height: ${{pct}}%;"></div>`
                    : '';
            }}).join('');
        }}

        function providerSummaryGrid(values, totalLabel, showMetadata = false, costScope = null, costKey = null) {{
            const cards = providerKeys.map(key => {{
                const agent = agentData[key] || {{}};
                const metadata = showMetadata
                    ? `<div style="color: #666; font-size: 0.75rem; margin-top: 6px;">${{agent.unique_messages || 0}} records · ${{agent.files || 0}} files</div>`
                    : '';
                const cost = costScope
                    ? `<div style="color: #aaa; font-size: 0.72rem; margin-top: 6px;">${{formatBackendCost(costRollup(key, costScope, costKey))}}</div>`
                    : '';
                return `
                    <div class="summary-item provider-${{key}}">
                        <div class="summary-label">${{providerLabel(key)}}</div>
                        <div class="summary-value provider-${{key}}">${{formatTokens(values[key] || 0)}}</div>
                        ${{metadata}}
                        ${{cost}}
                    </div>
                `;
            }}).join('');
            return `
                <div class="summary-grid">
                    ${{cards}}
                    <div class="summary-item">
                        <div class="summary-label">${{totalLabel}}</div>
                        <div class="summary-value total">${{formatTokens(sumProviderValues(values))}}</div>
                        ${{costScope ? `<div style="color: #aaa; font-size: 0.72rem; margin-top: 6px;">${{formatBackendCost(costRollup(null, costScope, costKey))}}</div>` : ''}}
                    </div>
                </div>
            `;
        }}

        function aggregateByMonth(year, month) {{
            const prefix = `${{year}}-${{String(month).padStart(2, '0')}}`;
            const result = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            for (const [date, usage] of Object.entries(dailyData)) {{
                if (date.startsWith(prefix)) {{
                    result.input_tokens += usage.input_tokens || 0;
                    result.output_tokens += usage.output_tokens || 0;
                    result.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                    result.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;
                }}
            }}
            return result;
        }}

        function aggregateByYear(year) {{
            const prefix = `${{year}}-`;
            const result = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            for (const [date, usage] of Object.entries(dailyData)) {{
                if (date.startsWith(prefix)) {{
                    result.input_tokens += usage.input_tokens || 0;
                    result.output_tokens += usage.output_tokens || 0;
                    result.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                    result.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;
                }}
            }}
            return result;
        }}

        function aggregateAllTime() {{
            const result = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            for (const usage of Object.values(dailyData)) {{
                result.input_tokens += usage.input_tokens || 0;
                result.output_tokens += usage.output_tokens || 0;
                result.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                result.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;
            }}
            return result;
        }}

        function formatHour(hour) {{
            const h = parseInt(hour);
            if (h === 0) return '12a';
            if (h < 12) return h + 'a';
            if (h === 12) return '12p';
            return (h - 12) + 'p';
        }}

        function formatHourFull(hour) {{
            const h = parseInt(hour);
            if (h === 0) return '12:00 AM';
            if (h < 12) return h + ':00 AM';
            if (h === 12) return '12:00 PM';
            return (h - 12) + ':00 PM';
        }}

        function formatDateFull(dateStr) {{
            const d = new Date(dateStr + 'T12:00:00');
            const dayName = dayNames[d.getDay()];
            const monthName = monthNames[d.getMonth()];
            const day = d.getDate();
            const year = d.getFullYear();
            return `${{dayName}}, ${{monthName}} ${{day}}, ${{year}}`;
        }}

        function renderDaily() {{
            // Update navigation
            const dateIndex = dates.indexOf(currentDate);
            const canPrev = dateIndex > 0;
            const canNext = dateIndex < dates.length - 1;
            document.getElementById('nav-prev').classList.toggle('disabled', !canPrev);
            document.getElementById('nav-next').classList.toggle('disabled', !canNext);
            document.getElementById('nav-current').textContent = formatDateFull(currentDate);

            const dayUsage = dailyData[currentDate] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            const dayProviderValues = providerValuesForDate(currentDate);
            const dayTotal = breakdownMode === 'providers'
                ? sumProviderValues(dayProviderValues)
                : getTotalForSummary(dayUsage);
            const hourData = hourlyData[currentDate] || {{}};

            // Find max hour for scaling
            let maxHourTotal = 0;
            for (let h = 0; h < 24; h++) {{
                const hData = hourData[String(h)] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                const hTotal = breakdownMode === 'providers'
                    ? sumProviderValues(providerValuesForDate(currentDate, h))
                    : getTotal(hData);
                if (hTotal > maxHourTotal) maxHourTotal = hTotal;
            }}
            if (maxHourTotal === 0) maxHourTotal = 1;

            // Calculate nice Y-axis scale
            function niceNum(range, round) {{
                const exponent = Math.floor(Math.log10(range));
                const fraction = range / Math.pow(10, exponent);
                let niceFraction;
                if (round) {{
                    if (fraction < 1.5) niceFraction = 1;
                    else if (fraction < 3) niceFraction = 2;
                    else if (fraction < 7) niceFraction = 5;
                    else niceFraction = 10;
                }} else {{
                    if (fraction <= 1) niceFraction = 1;
                    else if (fraction <= 2) niceFraction = 2;
                    else if (fraction <= 5) niceFraction = 5;
                    else niceFraction = 10;
                }}
                return niceFraction * Math.pow(10, exponent);
            }}

            function formatAxisLabel(n) {{
                if (n >= 1e9) return (n / 1e9).toFixed(n % 1e9 === 0 ? 0 : 1) + 'G';
                if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 === 0 ? 0 : 1) + 'M';
                if (n >= 1e3) return (n / 1e3).toFixed(n % 1e3 === 0 ? 0 : 1) + 'K';
                return n.toString();
            }}

            const yAxisMax = niceNum(maxHourTotal, false);
            const tickCount = 5;
            const tickInterval = yAxisMax / (tickCount - 1);

            // Build Y-axis
            let yAxisHtml = '';
            for (let i = tickCount - 1; i >= 0; i--) {{
                const value = Math.round(tickInterval * i);
                yAxisHtml += `<div class="y-axis-label">${{formatAxisLabel(value)}}</div>`;
            }}
            document.getElementById('y-axis').innerHTML = yAxisHtml;

            // Use yAxisMax for scaling instead of maxHourTotal
            const scaleMax = yAxisMax;

            // Header
            document.getElementById('daily-header').innerHTML = `
                <div class="daily-total">${{formatTokens(dayTotal)}} tokens</div>
            `;

            // Build hourly chart
            let chartHtml = '';
            for (let h = 0; h < 24; h++) {{
                const hData = hourData[String(h)] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                const hourProviderValues = providerValuesForDate(currentDate, h);
                const hTotal = breakdownMode === 'providers'
                    ? sumProviderValues(hourProviderValues)
                    : getTotal(hData);
                const heightPct = hTotal > 0 ? Math.max(2, (hTotal / scaleMax) * 100) : 0;

                // Calculate segment heights proportionally
                const inputPct = hTotal > 0 ? ((hData.input_tokens || 0) / hTotal) * heightPct : 0;
                const outputPct = hTotal > 0 ? ((hData.output_tokens || 0) / hTotal) * heightPct : 0;
                const cacheReadPct = hTotal > 0 ? ((hData.cache_read_input_tokens || 0) / hTotal) * heightPct : 0;
                const cacheCreatePct = hTotal > 0 ? ((hData.cache_creation_input_tokens || 0) / hTotal) * heightPct : 0;
                const tooltipBreakdown = breakdownMode === 'providers'
                    ? providerBreakdownHtml(hourProviderValues, true) +
                      (costData ? `<br>${{providerCostBreakdownHtml('hourly', currentDate, h, true)}}` : '')
                    : `
                        <span class="in-label">In: ${{formatTokens(hData.input_tokens || 0)}}</span><br>
                        <span class="out-label">Out: ${{formatTokens(hData.output_tokens || 0)}}</span><br>
                        <span class="cache-r-label">CR: ${{formatTokens(hData.cache_read_input_tokens || 0)}}</span><br>
                        <span class="cache-c-label">CC: ${{formatTokens(hData.cache_creation_input_tokens || 0)}}</span>
                    `;
                const segmentsHtml = breakdownMode === 'providers'
                    ? providerBarSegments(hourProviderValues, hTotal)
                    : `
                        ${{cacheCreatePct > 0 ? `<div class="bar-segment cache-create" style="height: ${{(cacheCreatePct / heightPct) * 100}}%;"></div>` : ''}}
                        ${{cacheReadPct > 0 ? `<div class="bar-segment cache-read" style="height: ${{(cacheReadPct / heightPct) * 100}}%;"></div>` : ''}}
                        ${{outputPct > 0 ? `<div class="bar-segment output" style="height: ${{(outputPct / heightPct) * 100}}%;"></div>` : ''}}
                        ${{inputPct > 0 ? `<div class="bar-segment input" style="height: ${{(inputPct / heightPct) * 100}}%;"></div>` : ''}}
                    `;

                chartHtml += `
                    <div class="hour-bar-container">
                        <div class="hour-bar-wrapper">
                            <div class="hour-bar" style="height: ${{heightPct}}%;">
                                <div class="hour-bar-tooltip">
                                    <div class="tooltip-hour">${{formatHourFull(h)}}</div>
                                    <div class="tooltip-total">${{formatTokens(hTotal)}}</div>
                                    <div class="tooltip-breakdown">
                                        ${{tooltipBreakdown}}
                                    </div>
                                </div>
                                ${{segmentsHtml}}
                            </div>
                        </div>
                        <div class="hour-label">${{formatHour(h)}}</div>
                    </div>
                `;
            }}

            document.getElementById('hourly-chart').innerHTML = chartHtml;

            // Summary with legend
            const legendHtml = breakdownMode === 'providers'
                ? providerLegendHtml()
                : `
                    <div class="legend-item"><div class="legend-color input"></div>Input</div>
                    <div class="legend-item"><div class="legend-color output"></div>Output</div>
                    <div class="legend-item"><div class="legend-color cache-read"></div>Cache Read</div>
                    <div class="legend-item"><div class="legend-color cache-create"></div>Cache Create</div>
                `;
            const summaryGrid = breakdownMode === 'providers'
                ? `<div style="margin-top: 20px;">${{providerSummaryGrid(dayProviderValues, 'Day Total', false, 'daily', currentDate)}}</div>`
                : `
                    <div class="summary-grid" style="margin-top: 20px;">
                        <div class="summary-item">
                            <div class="summary-label">Input Tokens</div>
                            <div class="summary-value input">${{formatTokens(dayUsage.input_tokens || 0)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Output Tokens</div>
                            <div class="summary-value output">${{formatTokens(dayUsage.output_tokens || 0)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Cache Read</div>
                            <div class="summary-value cache-read">${{formatTokens(dayUsage.cache_read_input_tokens || 0)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Cache Create</div>
                            <div class="summary-value cache-create">${{formatTokens(dayUsage.cache_creation_input_tokens || 0)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Day Total</div>
                            <div class="summary-value total">${{formatTokens(dayTotal)}}</div>
                        </div>
                    </div>
                `;
            document.getElementById('daily-summary').innerHTML = `
                <div class="chart-legend">
                    ${{legendHtml}}
                </div>
                ${{summaryGrid}}
            `;
        }}

        function renderAllTime() {{
            const totals = aggregateAllTime();
            const providerTotals = aggregateProviderValues(() => true);
            const grandTotal = breakdownMode === 'providers'
                ? sumProviderValues(providerTotals)
                : getTotalForSummary(totals);
            const dates = Object.keys(dailyData).sort();
            const numDays = dates.length;

            let peakDay = '';
            let peakAmount = 0;
            for (const [date, usage] of Object.entries(dailyData)) {{
                const total = breakdownMode === 'providers'
                    ? sumProviderValues(providerValuesForDate(date))
                    : getTotal(usage);
                if (total > peakAmount) {{
                    peakAmount = total;
                    peakDay = date;
                }}
            }}

            const avgDaily = numDays > 0 ? Math.round(grandTotal / numDays) : 0;
            const dateRange = dates.length > 0 ? `${{dates[0]}} to ${{dates[dates.length-1]}}` : 'No data';

            // Header with date range
            document.getElementById('all-time-header').innerHTML = `
                <div class="all-time-date-range">${{dateRange}}</div>
                <div class="all-time-days-count">${{numDays}} days with data</div>
            `;

            // Row 1: Total | Avg Daily | Peak Day
            document.getElementById('all-time-stats').innerHTML = `
                <div class="stat-card">
                    <div class="stat-label">Total Tokens</div>
                    <div class="stat-value highlight">${{formatTokens(grandTotal)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Average Daily</div>
                    <div class="stat-value" style="color: #00d4ff;">${{formatTokens(avgDaily)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Peak Day</div>
                    <div class="stat-value" style="color: #00d4ff;">${{formatTokens(peakAmount)}}</div>
                    <div style="color: #666; font-size: 0.85rem; margin-top: 8px;">${{peakDay}}</div>
                </div>
            `;

            // Bottom row: token categories or provider breakdown.
            document.getElementById('all-time-summary').innerHTML = breakdownMode === 'providers'
                ? providerSummaryGrid(providerTotals, 'Grand Total', true, 'total', null)
                : `
                    <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Input Tokens</div>
                        <div class="summary-value input">${{formatTokens(totals.input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Output Tokens</div>
                        <div class="summary-value output">${{formatTokens(totals.output_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Read</div>
                        <div class="summary-value cache-read">${{formatTokens(totals.cache_read_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Create</div>
                        <div class="summary-value cache-create">${{formatTokens(totals.cache_creation_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Grand Total</div>
                        <div class="summary-value total">${{formatTokens(grandTotal)}}</div>
                    </div>
                    </div>
                `;
        }}

        function renderYearly() {{
            document.getElementById('nav-current').textContent = currentYear;
            document.getElementById('nav-prev').classList.toggle('disabled', currentYear <= minYear);
            document.getElementById('nav-next').classList.toggle('disabled', currentYear >= maxYear);

            let html = '';
            let yearTotal = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            const yearProviderTotals = aggregateProviderValues(date => date.startsWith(`${{currentYear}}-`));

            for (let m = 1; m <= 12; m++) {{
                const monthData = aggregateByMonth(currentYear, m);
                const monthPrefix = `${{currentYear}}-${{String(m).padStart(2, '0')}}`;
                const monthProviderValues = aggregateProviderValues(date => date.startsWith(monthPrefix));
                const total = breakdownMode === 'providers'
                    ? sumProviderValues(monthProviderValues)
                    : getTotalForSummary(monthData);
                const hasData = total > 0;
                const breakdownHtml = breakdownMode === 'providers'
                    ? providerBreakdownHtml(monthProviderValues, false, true) +
                      (costData ? `<br>${{providerCostBreakdownHtml('monthly', monthPrefix, null, false)}}` : '')
                    : `
                        <span class="in-label">In: ${{formatTokens(monthData.input_tokens)}}</span>
                        <span class="out-label">Out: ${{formatTokens(monthData.output_tokens)}}</span>
                        <span class="cache-r-label">Cache R: ${{formatTokens(monthData.cache_read_input_tokens)}}</span>
                        <span class="cache-c-label">Cache C: ${{formatTokens(monthData.cache_creation_input_tokens)}}</span>
                    `;

                yearTotal.input_tokens += monthData.input_tokens;
                yearTotal.output_tokens += monthData.output_tokens;
                yearTotal.cache_read_input_tokens += monthData.cache_read_input_tokens;
                yearTotal.cache_creation_input_tokens += monthData.cache_creation_input_tokens;

                html += `
                    <div class="month-card ${{hasData ? '' : 'no-data'}}" data-month="${{m}}" data-year="${{currentYear}}">
                        <div class="month-name">${{monthNames[m-1]}}</div>
                        <div class="month-total">${{hasData ? formatTokens(total) : '—'}}</div>
                        <div class="month-breakdown">
                            ${{breakdownHtml}}
                        </div>
                    </div>
                `;
            }}

            document.getElementById('year-grid').innerHTML = html;

            const grandTotal = breakdownMode === 'providers'
                ? sumProviderValues(yearProviderTotals)
                : getTotalForSummary(yearTotal);
            const yearSummary = breakdownMode === 'providers'
                ? providerSummaryGrid(yearProviderTotals, 'Year Total', false, 'yearly', String(currentYear))
                : `
                <h2>${{currentYear}} Yearly Summary</h2>
                <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Input Tokens</div>
                        <div class="summary-value input">${{formatTokens(yearTotal.input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Output Tokens</div>
                        <div class="summary-value output">${{formatTokens(yearTotal.output_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Read</div>
                        <div class="summary-value cache-read">${{formatTokens(yearTotal.cache_read_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Create</div>
                        <div class="summary-value cache-create">${{formatTokens(yearTotal.cache_creation_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Grand Total</div>
                        <div class="summary-value total">${{formatTokens(grandTotal)}}</div>
                    </div>
                </div>
                `;
            document.getElementById('year-summary').innerHTML = breakdownMode === 'providers'
                ? `<h2>${{currentYear}} Yearly Summary</h2>${{yearSummary}}`
                : yearSummary;

            // Add click handlers for month cards
            document.querySelectorAll('.month-card:not(.no-data)').forEach(card => {{
                card.addEventListener('click', () => {{
                    currentMonth = parseInt(card.dataset.month);
                    currentYear = parseInt(card.dataset.year);
                    document.querySelector('.nav-tab[data-view="monthly"]').click();
                }});
            }});
        }}

        function getDaysInMonth(year, month) {{
            return new Date(year, month, 0).getDate();
        }}

        function getFirstDayOfMonth(year, month) {{
            // Returns 0=Sun, 1=Mon, etc.
            return new Date(year, month - 1, 1).getDay();
        }}

        function renderMonthly() {{
            document.getElementById('nav-current').textContent = `${{monthNames[currentMonth-1]}} ${{currentYear}}`;

            // Determine if we can go prev/next
            const canPrev = currentYear > minYear || (currentYear === minYear && currentMonth > 1);
            const canNext = currentYear < maxYear || (currentYear === maxYear && currentMonth < 12);
            document.getElementById('nav-prev').classList.toggle('disabled', !canPrev);
            document.getElementById('nav-next').classList.toggle('disabled', !canNext);

            const daysInMonth = getDaysInMonth(currentYear, currentMonth);
            const firstDay = getFirstDayOfMonth(currentYear, currentMonth);

            // Previous month info
            const prevMonth = currentMonth === 1 ? 12 : currentMonth - 1;
            const prevYear = currentMonth === 1 ? currentYear - 1 : currentYear;
            const daysInPrevMonth = getDaysInMonth(prevYear, prevMonth);

            // Next month info
            const nextMonth = currentMonth === 12 ? 1 : currentMonth + 1;
            const nextYear = currentMonth === 12 ? currentYear + 1 : currentYear;
            const monthPrefix = `${{currentYear}}-${{String(currentMonth).padStart(2, '0')}}`;
            const monthlyProviderTotals = aggregateProviderValues(date => date.startsWith(monthPrefix));

            // Find max for intensity scaling
            let maxTotal = 0;
            for (let d = 1; d <= daysInMonth; d++) {{
                const dateKey = `${{currentYear}}-${{String(currentMonth).padStart(2,'0')}}-${{String(d).padStart(2,'0')}}`;
                const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                const total = breakdownMode === 'providers'
                    ? sumProviderValues(providerValuesForDate(dateKey))
                    : getTotal(usage);
                if (total > maxTotal) maxTotal = total;
            }}
            if (maxTotal === 0) maxTotal = 1;

            let html = `
                <div class="calendar-header">
                    <div class="header-cell">Sun</div>
                    <div class="header-cell">Mon</div>
                    <div class="header-cell">Tue</div>
                    <div class="header-cell">Wed</div>
                    <div class="header-cell">Thu</div>
                    <div class="header-cell">Fri</div>
                    <div class="header-cell">Sat</div>
                    <div class="header-cell">Weekly</div>
                </div>
            `;

            let day = 1;
            let nextMonthDay = 1;
            let monthlyTotals = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};

            // Calculate number of weeks needed
            const totalCells = firstDay + daysInMonth;
            const numWeeks = Math.ceil(totalCells / 7);

            // Generate weeks
            for (let week = 0; week < numWeeks; week++) {{
                html += '<div class="week-row">';
                let weekInputTotal = 0;
                let weekOutputTotal = 0;
                const weekProviderTotals = Object.fromEntries(providerKeys.map(key => [key, 0]));

                for (let dow = 0; dow < 7; dow++) {{
                    const cellIndex = week * 7 + dow;

                    if (cellIndex < firstDay) {{
                        // Previous month days
                        const prevDay = daysInPrevMonth - firstDay + 1 + cellIndex;
                        const dateKey = `${{prevYear}}-${{String(prevMonth).padStart(2,'0')}}-${{String(prevDay).padStart(2,'0')}}`;
                        const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                        const providerValues = providerValuesForDate(dateKey);
                        const total = breakdownMode === 'providers' ? sumProviderValues(providerValues) : getTotal(usage);
                        weekInputTotal += usage.input_tokens || 0;
                        weekOutputTotal += usage.output_tokens || 0;
                        addProviderValues(weekProviderTotals, providerValues);
                        const hasData = dates.includes(dateKey);

                        html += `
                            <div class="day-cell other-month intensity-low${{hasData ? ' clickable' : ''}}" data-date="${{dateKey}}">
                                <div class="day-header">
                                    <span class="day-total">${{formatTokens(total)}}</span>
                                    <span class="day-number">${{prevDay}}</span>
                                </div>
                                <div class="day-breakdown">
                                    ${{dateBreakdownHtml(dateKey, usage)}}
                                </div>
                            </div>
                        `;
                    }} else if (day <= daysInMonth) {{
                        // Current month days
                        const dateKey = `${{currentYear}}-${{String(currentMonth).padStart(2,'0')}}-${{String(day).padStart(2,'0')}}`;
                        const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                        const providerValues = providerValuesForDate(dateKey);
                        const total = breakdownMode === 'providers' ? sumProviderValues(providerValues) : getTotal(usage);
                        weekInputTotal += usage.input_tokens || 0;
                        weekOutputTotal += usage.output_tokens || 0;
                        addProviderValues(weekProviderTotals, providerValues);
                        const hasData = dates.includes(dateKey);

                        monthlyTotals.input_tokens += usage.input_tokens || 0;
                        monthlyTotals.output_tokens += usage.output_tokens || 0;
                        monthlyTotals.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                        monthlyTotals.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;

                        const intensity = total > 0 ? Math.min(5, Math.ceil((total / maxTotal) * 5)) : 0;
                        const intensityClass = intensity > 0 ? `intensity-${{intensity}}` : 'intensity-low';

                        html += `
                            <div class="day-cell ${{intensityClass}}${{hasData ? ' clickable' : ''}}" data-date="${{dateKey}}">
                                <div class="day-header">
                                    <span class="day-total">${{formatTokens(total)}}</span>
                                    <span class="day-number">${{day}}</span>
                                </div>
                                <div class="day-breakdown">
                                    ${{dateBreakdownHtml(dateKey, usage)}}
                                </div>
                            </div>
                        `;
                        day++;
                    }} else {{
                        // Next month days
                        const dateKey = `${{nextYear}}-${{String(nextMonth).padStart(2,'0')}}-${{String(nextMonthDay).padStart(2,'0')}}`;
                        const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                        const providerValues = providerValuesForDate(dateKey);
                        const total = breakdownMode === 'providers' ? sumProviderValues(providerValues) : getTotal(usage);
                        weekInputTotal += usage.input_tokens || 0;
                        weekOutputTotal += usage.output_tokens || 0;
                        addProviderValues(weekProviderTotals, providerValues);
                        const hasData = dates.includes(dateKey);

                        html += `
                            <div class="day-cell other-month intensity-low${{hasData ? ' clickable' : ''}}" data-date="${{dateKey}}">
                                <div class="day-header">
                                    <span class="day-total">${{formatTokens(total)}}</span>
                                    <span class="day-number">${{nextMonthDay}}</span>
                                </div>
                                <div class="day-breakdown">
                                    ${{dateBreakdownHtml(dateKey, usage)}}
                                </div>
                            </div>
                        `;
                        nextMonthDay++;
                    }}
                }}

                const weekTotal = breakdownMode === 'providers'
                    ? sumProviderValues(weekProviderTotals)
                    : weekInputTotal + weekOutputTotal;
                const weekBreakdown = breakdownMode === 'providers'
                    ? providerBreakdownHtml(weekProviderTotals, false, true)
                    : `
                        <span class="in-label">In: ${{formatTokens(weekInputTotal)}}</span>
                        <span class="out-label">Out: ${{formatTokens(weekOutputTotal)}}</span>
                    `;
                html += `
                    <div class="week-total">
                        <div class="week-total-label">Week Total</div>
                        <div class="week-total-value">${{formatTokens(weekTotal)}}</div>
                        <div class="day-breakdown" style="margin-top: 6px;">
                            ${{weekBreakdown}}
                        </div>
                    </div>
                </div>`;
            }}

            const grandTotal = breakdownMode === 'providers'
                ? sumProviderValues(monthlyProviderTotals)
                : getTotalForSummary(monthlyTotals);
            const monthlySummary = breakdownMode === 'providers'
                ? providerSummaryGrid(monthlyProviderTotals, 'Month Total', false, 'monthly', monthPrefix)
                : `
                    <div class="summary-grid">
                        <div class="summary-item">
                            <div class="summary-label">Input Tokens</div>
                            <div class="summary-value input">${{formatTokens(monthlyTotals.input_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Output Tokens</div>
                            <div class="summary-value output">${{formatTokens(monthlyTotals.output_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Cache Read</div>
                            <div class="summary-value cache-read">${{formatTokens(monthlyTotals.cache_read_input_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Cache Create</div>
                            <div class="summary-value cache-create">${{formatTokens(monthlyTotals.cache_creation_input_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Grand Total</div>
                            <div class="summary-value total">${{formatTokens(grandTotal)}}</div>
                        </div>
                    </div>
                `;
            html += `
                <div class="summary">
                    <h2>${{monthNames[currentMonth-1]}} ${{currentYear}} Summary</h2>
                    ${{monthlySummary}}
                </div>
            `;

            document.getElementById('monthly-calendar').innerHTML = html;

            // Add click handlers for day cells
            document.querySelectorAll('.day-cell.clickable').forEach(cell => {{
                cell.addEventListener('click', () => {{
                    currentDate = cell.dataset.date;
                    switchView('daily');
                }});
            }});
        }}

        function switchView(view) {{
            currentView = view;
            const costDashboard = document.getElementById('cost-dashboard-container');
            if (costDashboard) costDashboard.hidden = breakdownMode !== 'providers';

            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.querySelector(`.nav-tab[data-view="${{view}}"]`).classList.add('active');

            document.querySelectorAll('.view-content').forEach(v => v.classList.remove('active'));
            document.getElementById(`view-${{view}}`).classList.add('active');

            const subNav = document.getElementById('sub-nav');
            if (view === 'alltime') {{
                subNav.style.display = 'none';
                renderAllTime();
            }} else if (view === 'yearly') {{
                subNav.style.display = 'flex';
                renderYearly();
            }} else if (view === 'monthly') {{
                subNav.style.display = 'flex';
                renderMonthly();
            }} else if (view === 'daily') {{
                subNav.style.display = 'flex';
                renderDaily();
            }}
        }}

        // Event listeners
        document.querySelectorAll('.nav-tab').forEach(tab => {{
            tab.addEventListener('click', () => switchView(tab.dataset.view));
        }});

        document.querySelectorAll('.breakdown-option').forEach(option => {{
            option.addEventListener('click', () => {{
                breakdownMode = option.dataset.breakdown;
                document.querySelectorAll('.breakdown-option').forEach(button => button.classList.remove('active'));
                option.classList.add('active');
                switchView(currentView);
            }});
        }});

        document.getElementById('nav-prev').addEventListener('click', () => {{
            if (currentView === 'yearly' && currentYear > minYear) {{
                currentYear--;
                renderYearly();
            }} else if (currentView === 'monthly') {{
                currentMonth--;
                if (currentMonth < 1) {{
                    currentMonth = 12;
                    currentYear--;
                }}
                renderMonthly();
            }} else if (currentView === 'daily') {{
                const dateIndex = dates.indexOf(currentDate);
                if (dateIndex > 0) {{
                    currentDate = dates[dateIndex - 1];
                    renderDaily();
                }}
            }}
        }});

        document.getElementById('nav-next').addEventListener('click', () => {{
            if (currentView === 'yearly' && currentYear < maxYear) {{
                currentYear++;
                renderYearly();
            }} else if (currentView === 'monthly') {{
                currentMonth++;
                if (currentMonth > 12) {{
                    currentMonth = 1;
                    currentYear++;
                }}
                renderMonthly();
            }} else if (currentView === 'daily') {{
                const dateIndex = dates.indexOf(currentDate);
                if (dateIndex < dates.length - 1) {{
                    currentDate = dates[dateIndex + 1];
                    renderDaily();
                }}
            }}
        }});

        // Initial render
        switchView('daily');

        // Keyboard shortcuts
        const helpModal = document.getElementById('help-modal');
        const protip = document.getElementById('protip');

        function showHelp() {{
            helpModal.classList.add('active');
            protip.classList.add('hidden');
        }}

        function hideHelp() {{
            helpModal.classList.remove('active');
        }}

        function navigatePrev() {{
            document.getElementById('nav-prev').click();
        }}

        function navigateNext() {{
            document.getElementById('nav-next').click();
        }}

        function goToToday() {{
            // Reset to the latest date with data
            if (dates.length > 0) {{
                const latestDate = dates[dates.length - 1];
                currentDate = latestDate;
                currentYear = parseInt(latestDate.substring(0, 4));
                currentMonth = parseInt(latestDate.substring(5, 7));
            }}
            switchView('daily');
        }}

        document.addEventListener('keydown', (e) => {{
            // Ignore if typing in an input
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            const key = e.key;

            if (key === 'Escape') {{
                hideHelp();
                return;
            }}

            if (helpModal.classList.contains('active')) return;

            switch (key) {{
                case '?':
                    showHelp();
                    break;
                case '1':
                    switchView('daily');
                    break;
                case '2':
                    switchView('monthly');
                    break;
                case '3':
                    switchView('yearly');
                    break;
                case '4':
                    switchView('alltime');
                    break;
                case 'b':
                    document.querySelector(`.breakdown-option[data-breakdown="${{breakdownMode === 'tokens' ? 'providers' : 'tokens'}}"]`).click();
                    break;
                case 'h':
                case 'k':
                    navigatePrev();
                    break;
                case 'j':
                case 'l':
                    navigateNext();
                    break;
                case 't':
                    goToToday();
                    break;
            }}
        }});

        // Close modal when clicking overlay
        helpModal.addEventListener('click', (e) => {{
            if (e.target === helpModal) hideHelp();
        }});
    </script>
</body>
</html>
"""
    return html


def main():
    parser = argparse.ArgumentParser(
        description="Generate an interactive HTML calendar of AI agent token usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s
    %(prog)s --utc
    %(prog)s --tz-offset -8
    %(prog)s --no-open
    %(prog)s -o ~/report.html
    %(prog)s -q

How it works:
    1. Scans ~/ for Claude, Codex, Grok, and Composer session files
    2. Parses each agent's native usage records with record-level measurement quality
    3. Deduplicates streaming and cumulative usage snapshots
    4. Aggregates by date in your local timezone
    5. Generates an interactive HTML with four views:
       - Daily: Hour-by-hour usage and per-day summary
       - Monthly: Daily calendar view with weekly totals
       - Yearly: Month-by-month overview (click to drill down)
       - All Time: Overall statistics and token breakdown
    6. Opens the result in your default browser (unless --no-open)

Token types:
    - Input:        Tokens sent to the model (your prompts + context)
    - Output:       Tokens generated by the model (responses)
    - Cache Read:   Tokens read from prompt cache (saves cost)
    - Cache Create: Tokens written to prompt cache

Notes:
    - Uses your system's local timezone by default
    - Cursor/Composer quality is record-level: native cloud/team counters may be
      provider-reported, while local transcript token reconstruction is estimated
    - Color intensity on calendar cells reflects relative daily usage
    - Click month cards in yearly view to jump to that month
    - Parsed session data is cached under
      ~/.local/share/claude-usage-calendar/ (or XDG_DATA_HOME), one file per
      search path and timezone; the cache keeps usage from deleted session
      files; use --no-cache to report only files still on disk
        """,
    )
    parser.add_argument(
        "--utc", action="store_true", help="Use UTC instead of local time"
    )
    parser.add_argument(
        "--tz-offset",
        type=int,
        default=None,
        help="Custom timezone offset from UTC (e.g., -8 for PST)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="/tmp/claude_usage_calendar.html",
        help="Output HTML file path",
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Don't open the HTML file in browser"
    )
    parser.add_argument(
        "--search-path",
        type=str,
        default="~/",
        help="Path to search for supported agent session files (default: ~/)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the session parse cache",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON data instead of HTML calendar"
    )

    from costs.cli import CostCliError, configure_cost_cli, resolve_cost_cli

    configure_cost_cli(parser)

    args = parser.parse_args()

    try:
        cost_config = resolve_cost_cli(args)
    except CostCliError as error:
        parser.error(str(error))

    pricing_pack = None
    if cost_config.enabled:
        from costs.pricing import (
            PricingLoadOptions,
            PricingPackError,
            load_pricing_pack,
        )

        try:
            pricing_pack = load_pricing_pack(
                PricingLoadOptions(
                    path=cost_config.pricing_path,
                    use_builtin=cost_config.pricing_path is None,
                )
            ).pack
        except PricingPackError as error:
            parser.error(str(error))

    # Determine timezone
    if args.utc:
        tz = timezone.utc
        tz_label = "UTC"
    elif args.tz_offset is not None:
        tz = timezone(timedelta(hours=args.tz_offset))
        if args.tz_offset >= 0:
            tz_label = f"UTC+{args.tz_offset}"
        else:
            tz_label = f"UTC{args.tz_offset}"
    else:
        # Default: system local timezone
        tz = datetime.now().astimezone().tzinfo
        tz_label = datetime.now().astimezone().strftime("%Z")

    if not args.quiet:
        print(f"Finding agent session files in {args.search_path}...")

    files_by_agent = find_session_files(args.search_path)

    if not args.quiet:
        counts = ", ".join(
            f"{AGENT_DISPLAY_NAMES[agent]} {len(files_by_agent[agent])}"
            for agent in AGENT_NAMES
        )
        print(f"Found {sum(map(len, files_by_agent.values()))} files ({counts})")
        print("Parsing usage data...")

    cache_path = None if args.no_cache else default_cache_path(args.search_path, tz)
    parse_stats = {}
    daily_usage, hourly_usage, msg_count, agents = parse_session_files(
        files_by_agent, tz, cache_path, stats=parse_stats
    )

    if not args.quiet:
        historical_files = parse_stats.get("historical_files", 0)
        if historical_files > 0:
            print(
                f"Including usage from {historical_files} deleted session files"
            )
        print(f"Found {msg_count} unique usage records across {len(daily_usage)} days")

    # Build the canonical data structure
    usage_data = build_usage_data(
        daily_usage, hourly_usage, msg_count, tz_label, agents
    )

    cost_result = None
    if cost_config.enabled:
        from costs.serializer import attach_cost_estimates, serialize_cost_estimates

        try:
            raw_cost_records = extract_cost_raw_records(files_by_agent, tz)
            cost_result = calculate_cost_result(raw_cost_records, pricing_pack)
        except CostExtractionError as error:
            print(f"Cost extraction failed: {error}", file=sys.stderr)
            return 2
        usage_data = attach_cost_estimates(usage_data, cost_result)
        if cost_config.explain_costs:
            print(
                "Published-rate-equivalent cost explanation:\n"
                + json.dumps(serialize_cost_estimates(cost_result), indent=2),
                file=sys.stderr,
            )
        if (
            cost_config.require_complete_pricing
            and cost_result.rollup.status.value != "complete"
        ):
            print(
                "Cost pricing is incomplete: "
                f"status={cost_result.rollup.status.value}; "
                f"unpriced_components={len(cost_result.unpriced_quantities)}",
                file=sys.stderr,
            )
            return 2

    # JSON output mode
    if args.json:
        print(json.dumps(usage_data, indent=2))
        return 0

    if not args.quiet:
        print("Generating interactive HTML...")

    html = generate_html(usage_data, cost_result=cost_result)

    with open(args.output, "w") as f:
        f.write(html)

    if not args.quiet:
        print(f"Saved to {args.output}")

    if not args.no_open:
        subprocess.run(["open", args.output], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
