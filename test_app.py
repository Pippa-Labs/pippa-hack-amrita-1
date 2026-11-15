"""Contract and calculation tests, run in the publishing build."""

from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import (
    LocalRunner,
    OutputShapeViolation,
    Result,
)

from app import App, InvalidBudget

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

EXAMPLE = {
    "requests_per_month": 20000,
    "current_input_tokens": 8000,
    "output_tokens": 1000,
    "proposed_input_tokens": 3000,
    "input_price_per_million": 3,
    "output_price_per_million": 15,
    "currency": "USD",
}


def call(inputs, *, explicit=False):
    return LocalRunner(App()).call(
        "tokens.compare",
        inputs,
        granted_secrets={},
        now=NOW,
        request_id="test-1",
        explicit=explicit,
    ).result


def test_manifest():
    app = App()
    assert app.manifest.app_id == "hack.amrita_1"
    assert [o.id for o in app.manifest.outcomes] == ["tokens.compare"]


def test_worked_example():
    data = call(EXAMPLE).data
    assert data["current_monthly_input_tokens"] == 160000000
    assert data["proposed_monthly_input_tokens"] == 60000000
    assert data["monthly_output_tokens"] == 20000000
    assert data["current_total_monthly_tokens"] == 180000000
    assert data["proposed_total_monthly_tokens"] == 80000000
    assert data["current_monthly_cost"] == 780
    assert data["proposed_monthly_cost"] == 480
    assert data["monthly_savings"] == 300
    assert data["annualised_savings"] == 3600
    assert data["input_reduction_percent"] == 62.5
    assert data["total_token_reduction_percent"] == 55.56
    assert data["cost_reduction_percent"] == 38.46
    assert data["currency"] == "USD"


def test_missing_inputs_explain_what_is_needed():
    with pytest.raises(InvalidBudget) as failure:
        call({})
    assert "requests per month" in failure.value.user_message
    assert "input price per million" in failure.value.user_message


@pytest.mark.parametrize(
    "field,value",
    [
        ("requests_per_month", 0),
        ("requests_per_month", -1),
        ("requests_per_month", 1.5),
        ("current_input_tokens", -1),
        ("current_input_tokens", 2.5),
        ("output_tokens", -1),
        ("output_tokens", 1.5),
        ("proposed_input_tokens", -1),
        ("proposed_input_tokens", 1.5),
        ("input_price_per_million", -0.1),
        ("output_price_per_million", -0.1),
        ("requests_per_month", 1000000000001),
    ],
)
def test_invalid_numbers(field, value):
    with pytest.raises(InvalidBudget):
        call({**EXAMPLE, field: value})


def test_cost_only_uses_current_input_count():
    inputs = dict(EXAMPLE)
    del inputs["proposed_input_tokens"]
    data = call(inputs).data
    assert data["current_monthly_cost"] == 780
    assert data["proposed_monthly_cost"] == 780
    assert data["monthly_savings"] == 0
    assert "No proposed input count" in data["assumptions"]


def test_unchanged_usage():
    data = call({**EXAMPLE, "proposed_input_tokens": 8000}).data
    assert data["monthly_savings"] == 0
    assert data["cost_reduction_percent"] == 0


def test_increased_usage_reports_negative_savings():
    data = call({**EXAMPLE, "proposed_input_tokens": 10000}).data
    assert data["proposed_monthly_cost"] == 900
    assert data["monthly_savings"] == -120
    assert data["annualised_savings"] == -1440
    assert data["input_reduction_percent"] == -25


def test_zero_tokens_omit_undefined_percentages():
    data = call({
        **EXAMPLE,
        "current_input_tokens": 0,
        "proposed_input_tokens": 0,
        "output_tokens": 0,
    }).data
    assert data["current_monthly_cost"] == 0
    assert data["monthly_savings"] == 0
    assert "input_reduction_percent" not in data
    assert "total_token_reduction_percent" not in data
    assert "cost_reduction_percent" not in data
    assert "zero baseline" in data["assumptions"]


def test_zero_prices_keep_token_comparison():
    data = call({
        **EXAMPLE,
        "input_price_per_million": 0,
        "output_price_per_million": 0,
    }).data
    assert data["monthly_savings"] == 0
    assert data["input_reduction_percent"] == 62.5
    assert "cost_reduction_percent" not in data


def test_decimal_prices_and_rounding():
    data = call({
        "requests_per_month": 1000000,
        "current_input_tokens": 1,
        "proposed_input_tokens": 0,
        "output_tokens": 0,
        "input_price_per_million": 0.105,
        "output_price_per_million": 0,
    }).data
    assert data["current_monthly_cost"] == 0.11
    assert data["monthly_savings"] == 0.11
    # Annualise before rounding: 0.105 * 12 = 1.26.
    assert data["annualised_savings"] == 1.26


@pytest.mark.parametrize("currency", [None, ""])
def test_missing_currency_is_not_invented(currency):
    inputs = dict(EXAMPLE)
    if currency is None:
        del inputs["currency"]
    else:
        inputs["currency"] = currency
    data = call(inputs).data
    assert "currency" not in data
    assert "amounts are unlabelled" in data["assumptions"]


def test_explicit_selection():
    assert call(EXAMPLE, explicit=True).data == call(EXAMPLE).data
    with pytest.raises(InvalidBudget):
        call({}, explicit=True)


def test_runner_enforces_output_contract(monkeypatch):
    monkeypatch.setattr(
        App, "handle",
        lambda self, call: Result(data={"surprise": "hello"}),
    )
    with pytest.raises(OutputShapeViolation):
        call(EXAMPLE)
