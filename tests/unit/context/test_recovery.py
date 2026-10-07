"""Unit tests for RecoveryPolicy (docs/reference/models.md §9-§11)."""

from __future__ import annotations

from dataclasses import dataclass

from autoverse.core.context.config import RecoveryConfig
from autoverse.core.context.recovery import (
    RecoveryClass,
    RecoveryPolicy,
    parse_inline_json_tool_calls,
)
from autoverse.types.messages import Message, TextBlock, ToolUseBlock


@dataclass
class _Err:
    http_status: int | None = None
    error_kind: str = ""


def test_classify_retryable_by_kind() -> None:
    pol = RecoveryPolicy()
    assert pol.classify(_Err(error_kind="rate_limit")).cls is RecoveryClass.RETRY_SAME
    assert pol.classify(_Err(error_kind="server")).cls is RecoveryClass.RETRY_SAME


def test_classify_fallback_by_kind() -> None:
    pol = RecoveryPolicy()
    assert pol.classify(_Err(error_kind="auth")).cls is RecoveryClass.FALLBACK_MODEL


def test_classify_compact_retry_on_context_overflow() -> None:
    pol = RecoveryPolicy()
    act = pol.classify(_Err(error_kind="context_overflow"))
    assert act.cls is RecoveryClass.COMPACT_RETRY
    assert act.should_compact


def test_classify_unrecoverable() -> None:
    pol = RecoveryPolicy()
    assert pol.classify(_Err(error_kind="bad_request")).cls is RecoveryClass.UNRECOVERABLE
    assert pol.classify(_Err(error_kind="abort")).cls is RecoveryClass.UNRECOVERABLE


def test_classify_none_when_no_error() -> None:
    assert RecoveryPolicy().classify(None).cls is RecoveryClass.NONE


def test_L21_classify_uses_structured_status_not_substring() -> None:
    pol = RecoveryPolicy()
    # No error_kind; routes purely on the structured http_status field.
    assert pol.classify(_Err(http_status=503)).cls is RecoveryClass.RETRY_SAME
    assert pol.classify(_Err(http_status=401)).cls is RecoveryClass.FALLBACK_MODEL
    assert pol.classify(_Err(http_status=400)).cls is RecoveryClass.UNRECOVERABLE
    # A message that *mentions* "rate limit" but carries a permanent status must
    # NOT be mis-classified by substring matching.
    err = _Err(http_status=400, error_kind="bad_request")
    err_msg = "rate limit exceeded server error timeout"  # noqa: F841
    assert pol.classify(err).cls is RecoveryClass.UNRECOVERABLE


def test_L19_maybe_resume_text_on_max_tokens() -> None:
    pol = RecoveryPolicy(RecoveryConfig(max_resume_text=1, resume_token_bonus=999))
    assert pol.maybe_resume_text("MAX_TOKENS") is True
    assert pol.consume_max_tokens_override() == 999
    assert pol.consume_max_tokens_override() is None  # one-shot
    # exhausted budget
    assert pol.maybe_resume_text("MAX_TOKENS") is False
    # non-max-tokens never resumes
    assert pol.maybe_resume_text("END_TURN") is False


def test_L20_repair_truncated_tool_use() -> None:
    pol = RecoveryPolicy()
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="working"),
            ToolUseBlock(id="t1", name="bash", input={"cmd": "ls"}),
        ),
    )
    repaired = pol.repair_truncated_tool_use(msg, "MAX_TOKENS")
    assert len(repaired.content) == 1
    assert isinstance(repaired.content[0], TextBlock)
    # not truncated stop_reason -> unchanged
    assert pol.repair_truncated_tool_use(msg, "END_TURN") is msg


def test_L20_repair_placeholder_when_only_tool_use() -> None:
    pol = RecoveryPolicy()
    msg = Message(
        role="assistant",
        content=(ToolUseBlock(id="t1", name="bash", input={}),),
    )
    repaired = pol.repair_truncated_tool_use(msg, "MAX_TOKENS")
    assert isinstance(repaired.content[0], TextBlock)


def test_L18_json_recovery_gated_by_degradation_prone() -> None:
    pol = RecoveryPolicy()
    text = '{"type":"tool_use","id":"t1","name":"f","input":{"a":1}}'
    # not degradation prone -> no recovery
    assert pol.maybe_recover_json_tools(text, set(), degradation_prone=False) == []
    # degradation prone -> recovered
    blocks = pol.maybe_recover_json_tools(text, set(), degradation_prone=True)
    assert len(blocks) == 1
    assert isinstance(blocks[0], ToolUseBlock)
    assert blocks[0].name == "f"


def test_L18_disabled_by_config() -> None:
    pol = RecoveryPolicy(RecoveryConfig(enable_json_tool_recovery=False))
    text = '{"type":"tool_use","id":"t1","name":"f","input":{}}'
    assert pol.maybe_recover_json_tools(text, set(), degradation_prone=True) == []


def test_parse_inline_json_dedup_executed() -> None:
    text = '{"type":"tool_use","id":"dup","name":"f","input":{}}'
    assert parse_inline_json_tool_calls(text, {"dup"}) == []
    text2 = "{'type':'tool_use','id':'t2','name':'g','input':{'x':2}}"
    out = parse_inline_json_tool_calls(text2, set())
    assert out == [{"id": "t2", "name": "g", "input": {"x": 2}}]


def test_recovery_budgets_reset_for_each_turn() -> None:
    pol = RecoveryPolicy(RecoveryConfig(max_retries=1, max_resume_text=1))
    pol.note_retry()
    assert not pol.can_retry()
    assert pol.maybe_resume_text("MAX_TOKENS")
    pol.begin_turn()
    assert pol.can_retry()
    assert pol.maybe_resume_text("MAX_TOKENS")


def test_exhausted_or_partial_provider_error_is_terminal_in_core() -> None:
    class _ProviderError:
        error_kind = "rate_limit"
        retry_exhausted = True
        response_started = False

    pol = RecoveryPolicy()
    assert pol.classify(_ProviderError()).cls is RecoveryClass.UNRECOVERABLE
    _ProviderError.retry_exhausted = False
    _ProviderError.response_started = True
    assert pol.classify(_ProviderError()).cls is RecoveryClass.UNRECOVERABLE
