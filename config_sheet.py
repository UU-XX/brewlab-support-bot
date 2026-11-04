"""Google Sheets settings and per-message audit logs.

The settings tab maps column A keys to column B values. The log tab has ten
columns and one row per processed message. Invalid settings pause processing.
Uncertain writes require reconciliation instead of an automatic duplicate."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from retry_utils import AmbiguousOutcomeError, call_with_retry

logger = logging.getLogger(__name__)

# Defaults used by the settings template.
DEFAULT_MODE = "draft"
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_ACTIVE_DAYS = "mon,tue,wed,thu,fri"
DEFAULT_ACTIVE_HOURS = "09:00-18:00"
DEFAULT_TONE = "friendly, concise, professional"
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_HANDOFF_KEYWORDS = (
    "\u6295\u8bc9,\u9000\u6b3e,\u627e\u771f\u4eba,human,refund,complaint,lawyer,\u5f8b\u5e08,manager,\u7ecf\u7406"
)
DEFAULT_MODEL = "deepseek-flash"

VALID_MODES = ("draft", "live", "off")

WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "\u5468\u4e00": 0, "\u661f\u671f\u4e00": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "\u5468\u4e8c": 1, "\u661f\u671f\u4e8c": 1,
    "wed": 2, "wednesday": 2, "\u5468\u4e09": 2, "\u661f\u671f\u4e09": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "\u5468\u56db": 3, "\u661f\u671f\u56db": 3,
    "fri": 4, "friday": 4, "\u5468\u4e94": 4, "\u661f\u671f\u4e94": 4,
    "sat": 5, "saturday": 5, "\u5468\u516d": 5, "\u661f\u671f\u516d": 5,
    "sun": 6, "sunday": 6, "\u5468\u65e5": 6, "\u661f\u671f\u65e5": 6, "\u5468\u5929": 6,
}

# Ten columns in the first row of the log tab.
LOG_HEADERS = [
    "timestamp",          # Processing timestamp in ISO 8601 with time zone.
    "sender",             # Sender email address.
    "intent",             # Classified intent.
    "mode",               # Handling mode: draft, live or off.
    "handoff",            # Human handoff: yes or no.
    "reply_summary",      # Reply summary.
    "elapsed_ms",         # Elapsed time in milliseconds.
    "error",              # Error details.
    "thread_id",          # Original thread ID.
    "message_id",         # Original message ID for reconciliation.
]


class ConfigError(Exception):
    """Invalid or unavailable configuration; the caller must pause processing."""


@dataclass
class Settings:
    mode: str = DEFAULT_MODE
    timezone: str = DEFAULT_TIMEZONE
    active_days: str = DEFAULT_ACTIVE_DAYS
    active_hours: str = DEFAULT_ACTIVE_HOURS
    tone: str = DEFAULT_TONE
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    handoff_keywords: List[str] = field(default_factory=list)
    colleague_email: str = ""
    admin_email: str = ""
    model: str = DEFAULT_MODEL

    def active_weekdays(self) -> set[int]:
        return parse_active_days(self.active_days)

    def hours_range(self) -> tuple:
        return parse_active_hours(self.active_hours)

    def keyword_list(self) -> List[str]:
        if isinstance(self.handoff_keywords, str):
            return [k.strip().lower() for k in self.handoff_keywords.split(",") if k.strip()]
        return [str(k).strip().lower() for k in self.handoff_keywords if str(k).strip()]


def parse_active_days(raw: str) -> set:
    """Parse English or Chinese weekday names; reject empty or invalid input."""
    days = set()
    for token in str(raw or "").replace("，", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in WEEKDAY_ALIASES:
            days.add(WEEKDAY_ALIASES[token])
        else:
            raise ConfigError(f"Unrecognized weekday in active_days: {token!r}")
    if not days:
        raise ConfigError("active_days must not be empty")
    return days


def parse_active_hours(raw: str) -> tuple:
    """Parse a same-day interval such as 09:00-18:00 into minutes."""
    text = str(raw or "").strip()
    if "-" not in text:
        raise ConfigError(f"active_hours must look like 09:00-18:00; received {raw!r}")
    start_s, end_s = text.split("-", 1)
    start = _hhmm_to_minutes(start_s)
    end = _hhmm_to_minutes(end_s)
    if start >= end:
        raise ConfigError(f"active_hours must start before it ends: {raw!r}")
    return start, end


def _hhmm_to_minutes(text: str) -> int:
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ConfigError(f"Time must use HH:MM; received {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ConfigError(f"Time must use HH:MM; received {text!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"Time is out of range: {text!r}")
    return hour * 60 + minute


def is_within_active_window(settings: Settings, now: Optional[datetime] = None) -> bool:
    """Check configured weekdays, hours and time zone."""
    try:
        tz = ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Invalid time zone: {settings.timezone!r}") from exc
    current = now.astimezone(tz) if now else datetime.now(tz)
    if current.weekday() not in settings.active_weekdays():
        return False
    start, end = settings.hours_range()
    minutes = current.hour * 60 + current.minute
    return start <= minutes < end


def row_to_dict(values: Sequence[Sequence[Any]]) -> Dict[str, str]:
    """Normalize settings rows into lowercase, trimmed keys."""
    result: Dict[str, str] = {}
    for row in values or []:
        if not row:
            continue
        key = str(row[0]).strip().lower() if row[0] is not None else ""
        if not key:
            continue
        value = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        result[key] = value
    return result


def build_settings(raw: Dict[str, str]) -> Settings:
    """Build validated Settings from key-value pairs."""
    mode = (raw.get("mode") or DEFAULT_MODE).strip().lower() or DEFAULT_MODE
    if mode not in VALID_MODES:
        raise ConfigError(f"mode must be one of {VALID_MODES}; received {mode!r}")

    tz_name = (raw.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Invalid timezone: {tz_name!r}") from exc

    threshold_raw = (raw.get("confidence_threshold") or "").strip()
    if threshold_raw:
        try:
            threshold = float(threshold_raw)
        except ValueError as exc:
            raise ConfigError(f"confidence_threshold must be numeric; received {threshold_raw!r}") from exc
        if not 0.0 <= threshold <= 1.0:
            raise ConfigError("confidence_threshold must be between 0 and 1")
    else:
        threshold = DEFAULT_CONFIDENCE_THRESHOLD

    keywords_raw = raw.get("handoff_keywords")
    if keywords_raw is None or keywords_raw.strip() == "":
        keywords = DEFAULT_HANDOFF_KEYWORDS
    else:
        keywords = keywords_raw

    settings = Settings(
        mode=mode,
        timezone=tz_name,
        active_days=(raw.get("active_days") or DEFAULT_ACTIVE_DAYS).strip(),
        active_hours=(raw.get("active_hours") or DEFAULT_ACTIVE_HOURS).strip(),
        tone=(raw.get("tone") or DEFAULT_TONE).strip(),
        confidence_threshold=threshold,
        handoff_keywords=[
            k.strip().lower() for k in str(keywords).replace("，", ",").split(",") if k.strip()
        ],
        colleague_email=(raw.get("colleague_email") or "").strip(),
        admin_email=(raw.get("admin_email") or "").strip(),
        model=(raw.get("model") or os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
               or ("gpt-4o-mini" if os.environ.get("LLM_PROVIDER") == "openai" else DEFAULT_MODEL)).strip() or DEFAULT_MODEL,
    )
    # Validate before processing to avoid discovering bad settings mid-batch.
    settings.active_weekdays()
    settings.hours_range()
    return settings


def match_handoff_keyword(text: str, keywords: Sequence[str]) -> Optional[str]:
    """Return the first matching lowercase keyword, or None."""
    lowered = (text or "").lower()
    for keyword in keywords:
        key = str(keyword).strip().lower()
        if key and key in lowered:
            return key
    return None


class ConfigSheet:
    """Google Sheets adapter with an injectable service for tests."""

    def __init__(
        self,
        service: Any,
        spreadsheet_id: str,
        settings_range: str = "settings!A1:B50",
        log_range: str = "log!A1:J",
        sleep: Any = time.sleep,
    ) -> None:
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self.settings_range = settings_range
        self.log_range = log_range
        # Production uses real exponential backoff; tests may inject a no-op sleeper.
        self._sleep = time.sleep if sleep is None else sleep

    # ---------- Reading ----------

    def load_settings(self) -> Settings:
        """Read settings with up to two retries."""
        values = call_with_retry(
            lambda: self._get_values(self.settings_range),
            operation="Sheets read settings",
            sleep=self._sleep,
        )
        if not values:
            raise ConfigError("The settings tab is empty; fill in the configuration template")
        return build_settings(row_to_dict(values))

    def _get_values(self, cells: str) -> List[List[Any]]:
        """Read a range such as settings!A1:B.

        An open-ended range includes rows beyond 1000. Follow a nextRange value
        when supplied by an adapter, without re-reading the same range."""
        collected: List[List[Any]] = []
        seen_ranges: set[str] = set()
        rng = cells
        while rng and rng not in seen_ranges:
            seen_ranges.add(rng)
            req = (
                self.service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=rng)
            )
            result = req.execute()
            rows = result.get("values", []) or []
            if not rows:
                break
            collected.extend(rows)
            # Continue only when the response explicitly provides another range.
            rng = str(result.get("nextRange") or "")
        return collected

    def has_log_for_message(self, message_id: str, max_rows: int = 5000) -> bool:
        """Check message_id before recovering a pending log append.

        Search the complete J column. max_rows is retained for compatibility.
        If the lookup fails, the caller must not blindly append another row."""
        # Read all message IDs without duplicating the sheet prefix or omitting later rows.
        sheet = self.log_range.split("!", 1)[0]
        values = self._get_values(f"{sheet}!J:J")
        target = str(message_id or "")
        if not target:
            return False
        return any(row and str(row[0]) == target for row in values)

    # ---------- Writing logs ----------

    def append_log_row(self, row: Sequence[Any]) -> None:
        """Append through one retry layer; never repeat an uncertain write."""
        if len(row) != len(LOG_HEADERS):
            raise ValueError(f"A log row requires {len(LOG_HEADERS)} fields; received {len(row)}")
        try:
            self.append_rows([row])
        except AmbiguousOutcomeError as exc:
            # The row may already exist; preserve pending work for reconciliation.
            raise AmbiguousOutcomeError(f"Uncertain log append outcome: {exc}") from exc

    def append_rows(self, rows: Sequence[Sequence[Any]]) -> None:
        """Append a log row with write-aware retry handling.

        Transport timeouts become AmbiguousOutcomeError. The caller must
        reconcile the outcome before attempting another append."""
        body = {"values": [list(r) for r in rows]}
        call_with_retry(
            lambda: self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=self.log_range,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body=body,
            )
            .execute(),
            operation="Sheets append log",
            write=True,
            sleep=self._sleep,
        )

    def ensure_log_headers(self) -> None:
        """Write headers only when the first log row is empty."""
        result = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range="log!A1:J1")
            .execute()
        )
        rows = result.get("values", []) or []
        if rows and rows[0] and str(rows[0][0]).strip():
            return
        self.append_rows([LOG_HEADERS])


def _col_range(cell_range: str) -> str:
    """Extract A1:J from a qualified range such as log!A1:J."""
    return cell_range.split("!", 1)[1] if "!" in cell_range else cell_range


def build_log_row(
    timestamp: str,
    sender: str,
    intent: str,
    mode: str,
    handoff: bool,
    reply_summary: str,
    elapsed_ms: int,
    error: str = "",
    thread_id: str = "",
    message_id: str = "",
) -> List[Any]:
    """Build a row in LOG_HEADERS order."""
    return [
        timestamp,
        sender,
        intent,
        mode,
        "yes" if handoff else "no",
        reply_summary,
        elapsed_ms,
        error,
        thread_id,
        message_id,
    ]
