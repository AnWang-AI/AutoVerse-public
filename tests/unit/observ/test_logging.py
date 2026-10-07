"""Unit tests for process logging diagnostics (docs/reference/events-and-storage.md §3)."""

from __future__ import annotations

import json
import logging

from autoverse.infra.observ.logging import configure_logging, log_context


def test_configure_logging_writes_json_with_context(tmp_path) -> None:
    log_path = configure_logging(log_dir=tmp_path, frontend="test", level="INFO")
    logger = logging.getLogger("autoverse.test.logging")

    with log_context(session_id="sess-log", frontend="unit"):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("diagnostic message")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    record = json.loads(lines[-1])
    assert record["level"] == "ERROR"
    assert record["logger"] == "autoverse.test.logging"
    assert record["session_id"] == "sess-log"
    assert record["frontend"] == "unit"
    assert record["message"] == "diagnostic message"
    assert "RuntimeError: boom" in record["exception"]
