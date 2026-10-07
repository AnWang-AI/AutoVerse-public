"""Browser tool schemas reject ambiguous operations before any backend call."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from autoverse.core.tools.builtin.browser import (
    BrowserClickArgs,
    BrowserClosePageArgs,
    BrowserEvaluateArgs,
    BrowserGetContentArgs,
    BrowserKeyboardArgs,
    BrowserNewPageArgs,
    BrowserNavigateArgs,
    BrowserScrollArgs,
    BrowserSelectArgs,
    BrowserSwitchPageArgs,
    BrowserWaitArgs,
)


@pytest.mark.parametrize(
    ("args_type", "payload"),
    [
        (BrowserNavigateArgs, {"url": "https://example.com", "wait_until": "commit"}),
        (BrowserClickArgs, {"selector": "#x", "button": "primary"}),
        (BrowserClickArgs, {"selector": "#x", "screenshot": "sometimes"}),
        (BrowserGetContentArgs, {"mode": "json"}),
        (BrowserScrollArgs, {"direction": "diagonal"}),
        (BrowserScrollArgs, {"amount": -1}),
        (BrowserWaitArgs, {"condition": "visible"}),
        (BrowserWaitArgs, {"condition": "settled"}),
        (BrowserSelectArgs, {"selector": "#choice"}),
        (
            BrowserSelectArgs,
            {"selector": "#choice", "label": "One", "value": "1"},
        ),
        (BrowserKeyboardArgs, {"key": ""}),
        (BrowserEvaluateArgs, {"script": ""}),
    ],
)
def test_invalid_browser_arguments_fail_schema(args_type: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        args_type.model_validate(payload)


def test_valid_wait_and_select_arguments() -> None:
    assert BrowserWaitArgs(condition="visible", selector="#ready").selector == "#ready"
    assert BrowserWaitArgs(condition="networkidle").selector is None
    assert BrowserSelectArgs(selector="#choice", value="1").value == "1"


def test_select_ignores_blank_optional_choice_fields() -> None:
    args = BrowserSelectArgs(
        selector="#choice",
        label="  ",
        value="beta",
    )

    assert args.label is None
    assert args.value == "beta"


def test_browser_page_arguments_expose_stable_targets() -> None:
    assert BrowserNavigateArgs(url="https://example.com", page_id="page-a").page_id == "page-a"
    assert BrowserNewPageArgs(url="https://example.com").url == "https://example.com"
    assert BrowserSwitchPageArgs(page_id="page-b").page_id == "page-b"
    assert BrowserClosePageArgs().page_id is None


def test_browser_evaluate_schema_explains_async_and_result_contract() -> None:
    script_schema = BrowserEvaluateArgs.model_json_schema()["properties"]["script"]
    description = script_schema["description"]

    assert "return await" in description
    assert "JSON-serializable" in description
    assert "Promise" in description


def test_browser_screenshot_policy_defaults_and_legacy_booleans() -> None:
    screenshot_schema = BrowserNavigateArgs.model_json_schema()["properties"][
        "screenshot"
    ]
    assert screenshot_schema["enum"] == ["auto", "always", "never"]
    assert screenshot_schema["default"] == "auto"
    assert BrowserNavigateArgs(url="https://example.com").screenshot == "auto"
    assert (
        BrowserNavigateArgs(url="https://example.com", screenshot=True).screenshot
        == "always"
    )
    assert (
        BrowserNavigateArgs(url="https://example.com", screenshot=False).screenshot
        == "never"
    )
