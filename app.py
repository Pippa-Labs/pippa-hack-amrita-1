"""Estimate token costs using user-supplied usage and prices."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
MILLION = Decimal("1000000")
CENT = Decimal("0.01")

LABELS = {
    "requests_per_month": "requests per month",
    "current_input_tokens": "current input tokens per request",
    "output_tokens": "output tokens per request",
    "proposed_input_tokens": "proposed input tokens per request",
    "input_price_per_million": "input price per million tokens",
    "output_price_per_million": "output price per million tokens",
}

REQUIRED = (
    "requests_per_month",
    "current_input_tokens",
    "output_tokens",
    "input_price_per_million",
    "output_price_per_million",
)


class InvalidBudget(AgentAppError):
    user_message = "Please supply valid usage figures and token prices."
    retryable = False


def refuse(message: str) -> None:
    error = InvalidBudget(message)
    error.user_message = message
    raise error


def read_number(inputs: dict, name: str, *, whole: bool = False) -> Decimal:
    raw = inputs.get(name)
    label = LABELS[name]

    if raw is None or raw == "":
        refuse(f"Please provide {label}.")

    if isinstance(raw, bool):
        refuse(f"Please provide a number for {label}.")

    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        refuse(f"Please provide a number for {label}.")

    if not value.is_finite() or value < 0:
        refuse(f"Please provide a finite, non-negative number for {label}.")

    # Keep calculations and outputs within practical numeric limits.
    if value > Decimal("1000000000000"):
        refuse(f"Please provide {label} no greater than one trillion.")

    if whole and value != value.to_integral_value():
        refuse(f"Please provide a whole number for {label}.")

    return value


def rounded(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "tokens.compare":
            refuse("Please choose the token budget calculation.")

        inputs = call.inputs
        missing = [
            LABELS[name]
            for name in REQUIRED
            if inputs.get(name) is None or inputs.get(name) == ""
        ]
        if missing:
            refuse("Please provide: " + "; ".join(missing) + ".")

        with localcontext() as context:
            context.prec = 80
            return self._calculate(inputs)

    def _calculate(self, inputs: dict) -> Result:
        requests = read_number(inputs, "requests_per_month", whole=True)
        if requests == 0:
            refuse("Please provide requests per month greater than zero.")

        current = read_number(inputs, "current_input_tokens", whole=True)
        output = read_number(inputs, "output_tokens", whole=True)
        input_price = read_number(inputs, "input_price_per_million")
        output_price = read_number(inputs, "output_price_per_million")

        comparison_supplied = inputs.get("proposed_input_tokens") not in (None, "")
        proposed = (
            read_number(inputs, "proposed_input_tokens", whole=True)
            if comparison_supplied
            else current
        )

        current_input = requests * current
        proposed_input = requests * proposed
        monthly_output = requests * output
        current_total = current_input + monthly_output
        proposed_total = proposed_input + monthly_output

        current_cost = (
            current_input * input_price + monthly_output * output_price
        ) / MILLION
        proposed_cost = (
            proposed_input * input_price + monthly_output * output_price
        ) / MILLION
        savings = current_cost - proposed_cost

        data = {
            "current_monthly_input_tokens": int(current_input),
            "proposed_monthly_input_tokens": int(proposed_input),
            "monthly_output_tokens": int(monthly_output),
            "current_total_monthly_tokens": int(current_total),
            "proposed_total_monthly_tokens": int(proposed_total),
            "current_monthly_cost": rounded(current_cost),
            "proposed_monthly_cost": rounded(proposed_cost),
            "monthly_savings": rounded(savings),
            "annualised_savings": rounded(savings * 12),
        }

        undefined = []
        comparisons = (
            ("input_reduction_percent", current_input, proposed_input),
            ("total_token_reduction_percent", current_total, proposed_total),
            ("cost_reduction_percent", current_cost, proposed_cost),
        )
        for field, baseline, replacement in comparisons:
            if baseline:
                data[field] = rounded(
                    (baseline - replacement) / baseline * 100
                )
            else:
                undefined.append(field)

        currency = (inputs.get("currency") or "").strip()
        if currency:
            data["currency"] = currency

        assumptions = (
            "Request volume, output tokens per request and supplied prices "
            "remain unchanged. Both prices must use the same currency. "
            "Estimates exclude caching, discounts, retries, taxes and other "
            "fees. Annualised savings assume unchanged usage for 12 months. "
            "Positive savings mean a reduction; negative savings mean an "
            "increase. Shorter inputs may affect answer quality."
        )
        if not comparison_supplied:
            assumptions += (
                " No proposed input count was supplied, so the comparison "
                "uses the current count and shows no change."
            )
        if not currency:
            assumptions += " No currency was supplied; amounts are unlabelled."
        if undefined:
            assumptions += (
                " Percentages with a zero baseline are undefined and omitted: "
                + ", ".join(undefined)
                + "."
            )
        data["assumptions"] = assumptions

        prefix = f"{currency} " if currency else ""
        narrative = (
            f"Estimated monthly cost is {prefix}{current_cost:.2f}, "
            f"compared with {prefix}{proposed_cost:.2f} in the proposed scenario."
        )
        return Result(data=data, narrative=narrative)
