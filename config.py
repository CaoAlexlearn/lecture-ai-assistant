"""Environment-backed configuration for the Qwen classroom application."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Browser audio format used by the native WhisperLiveKit/Qwen stream.
SAMPLE_RATE = 16_000
CHANNELS = 1


class ConfigurationError(ValueError):
    """Raised when required configuration is absent or invalid."""


def _read_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{name} must be true/false (also accepts 1/0, yes/no, on/off)."
    )


def _read_optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    workspace_id: str | None
    send_workspace_header: bool
    source_language: str
    target_language: str
    debug: bool
    raw_event_debug: bool
    agent1_enabled: bool = False
    course_id: str = "default-course"
    lecture_id: str = "default-lecture"
    agent1_first_target_tokens: int = 300
    agent1_target_tokens: int = 700
    agent1_max_tokens: int = 1_800
    agent1_first_max_wait_seconds: float = 40.0
    agent1_max_wait_seconds: float = 60.0
    agent1_time_trigger_min_tokens: int = 100
    agent1_boundary_check_start_tokens: int = 900
    agent1_min_block_tokens: int = 450
    agent1_lookahead_reserve_tokens: int = 180
    agent1_recheck_growth_tokens: int = 200
    agent1_overlap_tokens: int = 120
    agent1_output_path: Path = Path("data")
    agent1_fake_sleep_seconds: float = 0.0
    agent1_max_attempts: int = 2
    agent1_shutdown_timeout_seconds: float = 30.0
    agent1_fake_fail_first_blocks: tuple[str, ...] = ()


def load_settings(*, require_api_key: bool = True) -> Settings:
    """Load .env (if present) and return validated application settings."""

    load_dotenv()

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if require_api_key and not api_key:
        raise ConfigurationError(
            "DASHSCOPE_API_KEY is missing. Copy .env.example to .env and set "
            "a Beijing-region Alibaba Cloud Model Studio API key."
        )

    source_language = os.getenv("SOURCE_LANGUAGE", "en").strip().lower()
    target_language = os.getenv("TARGET_LANGUAGE", "zh").strip().lower()
    if not source_language or not target_language:
        raise ConfigurationError("SOURCE_LANGUAGE and TARGET_LANGUAGE cannot be empty.")
    if source_language == target_language:
        raise ConfigurationError("SOURCE_LANGUAGE and TARGET_LANGUAGE must differ.")

    integer_defaults = {
        "AGENT1_FIRST_TARGET_TOKENS": 300,
        "AGENT1_TARGET_TOKENS": 700,
        "AGENT1_MAX_TOKENS": 1_800,
        "AGENT1_TIME_TRIGGER_MIN_TOKENS": 100,
        "AGENT1_BOUNDARY_CHECK_START_TOKENS": 900,
        "AGENT1_MIN_BLOCK_TOKENS": 450,
        "AGENT1_LOOKAHEAD_RESERVE_TOKENS": 180,
        "AGENT1_BOUNDARY_RECHECK_GROWTH_TOKENS": 200,
    }
    integer_values: dict[str, int] = {}
    for name, default in integer_defaults.items():
        configured = _read_optional_int(name)
        value = default if configured is None else configured
        if value <= 0:
            raise ConfigurationError(f"{name} must be positive.")
        integer_values[name] = value
    if (
        integer_values["AGENT1_TIME_TRIGGER_MIN_TOKENS"]
        > integer_values["AGENT1_MAX_TOKENS"]
    ):
        raise ConfigurationError(
            "AGENT1_TIME_TRIGGER_MIN_TOKENS cannot exceed AGENT1_MAX_TOKENS."
        )
    if (
        integer_values["AGENT1_MIN_BLOCK_TOKENS"]
        > integer_values["AGENT1_BOUNDARY_CHECK_START_TOKENS"]
    ):
        raise ConfigurationError(
            "AGENT1_MIN_BLOCK_TOKENS cannot exceed AGENT1_BOUNDARY_CHECK_START_TOKENS."
        )
    if (
        integer_values["AGENT1_BOUNDARY_CHECK_START_TOKENS"]
        > integer_values["AGENT1_MAX_TOKENS"]
    ):
        raise ConfigurationError(
            "AGENT1_BOUNDARY_CHECK_START_TOKENS cannot exceed AGENT1_MAX_TOKENS."
        )
    if (
        integer_values["AGENT1_LOOKAHEAD_RESERVE_TOKENS"]
        >= integer_values["AGENT1_MAX_TOKENS"]
    ):
        raise ConfigurationError(
            "AGENT1_LOOKAHEAD_RESERVE_TOKENS must be below AGENT1_MAX_TOKENS."
        )
    agent1_first_max_wait_seconds = _read_float("AGENT1_FIRST_MAX_WAIT_SECONDS", 40.0)
    agent1_max_wait_seconds = _read_float("AGENT1_MAX_WAIT_SECONDS", 60.0)
    if agent1_first_max_wait_seconds <= 0 or agent1_max_wait_seconds <= 0:
        raise ConfigurationError("Agent 1 wait times must be positive.")
    configured_overlap = _read_optional_int("AGENT1_OVERLAP_TOKENS")
    agent1_overlap_tokens = 120 if configured_overlap is None else configured_overlap
    if agent1_overlap_tokens < 0:
        raise ConfigurationError("AGENT1_OVERLAP_TOKENS cannot be negative.")
    agent1_fake_sleep_seconds = _read_float("AGENT1_FAKE_SLEEP_SECONDS", 0.0)
    if agent1_fake_sleep_seconds < 0:
        raise ConfigurationError("AGENT1_FAKE_SLEEP_SECONDS cannot be negative.")
    configured_max_attempts = _read_optional_int("AGENT1_MAX_ATTEMPTS")
    agent1_max_attempts = (
        2 if configured_max_attempts is None else configured_max_attempts
    )
    if agent1_max_attempts <= 0:
        raise ConfigurationError("AGENT1_MAX_ATTEMPTS must be positive.")
    agent1_shutdown_timeout_seconds = _read_float(
        "AGENT1_SHUTDOWN_TIMEOUT_SECONDS", 30.0
    )
    if agent1_shutdown_timeout_seconds <= 0:
        raise ConfigurationError("AGENT1_SHUTDOWN_TIMEOUT_SECONDS must be positive.")

    return Settings(
        api_key=api_key,
        workspace_id=os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip() or None,
        send_workspace_header=_read_bool("DASHSCOPE_SEND_WORKSPACE_HEADER"),
        source_language=source_language,
        target_language=target_language,
        debug=_read_bool("DEBUG"),
        raw_event_debug=_read_bool("RAW_EVENT_DEBUG"),
        agent1_enabled=_read_bool("AGENT1_ENABLED", default=False),
        course_id=os.getenv("COURSE_ID", "default-course").strip() or "default-course",
        lecture_id=os.getenv("LECTURE_ID", "default-lecture").strip()
        or "default-lecture",
        agent1_first_target_tokens=integer_values["AGENT1_FIRST_TARGET_TOKENS"],
        agent1_target_tokens=integer_values["AGENT1_TARGET_TOKENS"],
        agent1_max_tokens=integer_values["AGENT1_MAX_TOKENS"],
        agent1_first_max_wait_seconds=agent1_first_max_wait_seconds,
        agent1_max_wait_seconds=agent1_max_wait_seconds,
        agent1_time_trigger_min_tokens=integer_values["AGENT1_TIME_TRIGGER_MIN_TOKENS"],
        agent1_boundary_check_start_tokens=integer_values[
            "AGENT1_BOUNDARY_CHECK_START_TOKENS"
        ],
        agent1_min_block_tokens=integer_values["AGENT1_MIN_BLOCK_TOKENS"],
        agent1_lookahead_reserve_tokens=integer_values[
            "AGENT1_LOOKAHEAD_RESERVE_TOKENS"
        ],
        agent1_recheck_growth_tokens=integer_values[
            "AGENT1_BOUNDARY_RECHECK_GROWTH_TOKENS"
        ],
        agent1_overlap_tokens=agent1_overlap_tokens,
        agent1_output_path=Path(
            os.getenv("AGENT1_OUTPUT_PATH", "data").strip() or "data"
        ),
        agent1_fake_sleep_seconds=agent1_fake_sleep_seconds,
        agent1_max_attempts=agent1_max_attempts,
        agent1_shutdown_timeout_seconds=agent1_shutdown_timeout_seconds,
        agent1_fake_fail_first_blocks=tuple(
            item.strip()
            for item in os.getenv("AGENT1_FAKE_FAIL_FIRST_BLOCKS", "").split(",")
            if item.strip()
        ),
    )
