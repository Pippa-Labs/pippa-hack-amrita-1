"""Token Budget: guided collection of inputs and token-cost calculations."""

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
MAX_VALUE = Decimal("1000000000000")

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

WHOLE_NUMBERS = {
    "requests_per_month",
    "current_input_tokens",
    "output_tokens",
    "proposed_input_tokens",
}

QUESTIONS = {
    "requests_per_month": (
        "Roughly how many AI requests do you make per month? "
        "You can find this in your provider's usage dashboard or app logs. "
        "An estimate is fine if you identify it as an estimate."
    ),
    "current_input_tokens": (
        "What is your average input token count per request? "
        "This includes instructions, conversation history and documents "
        "sent to the model. Check your request usage logs. "
        "If you do not know, we can explore a hypothetical figure "
        "that you choose rather than treat it as measured usage."
    ),
    "output_tokens": (
        "What is your average output token count per request? "
        "These are tokens generated in the model's response. "
        "Check your request usage logs, or choose a clearly hypothetical "
        "figure to explore."
    ),
    "input_price_per_million": (
        "What input price per million tokens applies to your model? "
        "Please check your provider's pricing and include the currency. "
        "If quoted per thousand tokens, multiply that price by 1,000. "
        "Do not share API keys."
    ),
    "output_price_per_million": (
        "What output price per million tokens applies to your model? "
        "Use the same currency as the input price. "
        "If quoted per thousand tokens, multiply that price by 1,000."
    ),
}


class InvalidBudget(AgentAppError):
    user_message = "Please supply valid usage figures and token prices."
    retryable = False


def refuse(message: str) -> None:
    error = InvalidBudget(message)
    error.user_message = message
    raise error


def present(value) -> bool:
    return value is not None and value != ""


def read_number(inputs: dict, name: str) -> Decimal:
    raw = inputs.get(name)
    label = LABELS[name]

    if not present(raw):
        refuse(f"Please provide {label}.")

    if isinstance(raw, bool):
        refuse(f"Please provide a number for {label}.")

    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        refuse(f"Please provide a number for {label}.")

    if not value.is_finite() or value < 0:
        refuse(f"Please provide a finite, non-negative number for {label}.")

    if value > MAX_VALUE:
        refuse(f"Please provide {label} no greater than one trillion.")

    if name in WHOLE_NUMBERS and value != value.to_integral_value():
        refuse(f"Please provide a whole number for {label}.")

    if name == "requests_per_month" and value == 0:
        refuse("Please provide requests per month greater than zero.")

    return value


def rounded(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def supplied_summary(values: dict, currency: str) -> str:
    parts = [
        f"{LABELS[name]}: {format(value, 'f')}"
        for name, value in values.items()
    ]
    if currency:
        parts.append(f"currency: {currency}")
    return "; ".join(parts) if parts else "No figures supplied yet."


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "tokens.compare":
            refuse("Please choose the token budget calculation.")

        with localcontext() as context:
            context.prec = 80
            return self._respond(call.inputs)

    def _respond(self, inputs: dict) -> Result:
        # Validate any figures already supplied, even before collection is done.
        values = {
            name: read_number(inputs, name)
            for name in LABELS
            if present(inputs.get(name))
        }
        currency = (inputs.get("currency") or "").strip()
        summary = supplied_summary(values, currency)

        missing = [name for name in REQUIRED if name not in values]

        if missing:
            question = QUESTIONS[missing[0]]
            return Result(
                data={
                    "status": "needs_input",
                    "next_question": question,
                    "missing_inputs": ", ".join(missing),
                    "supplied_inputs": summary,
                },
                narrative=question,
            )

        return self._calculate(values, currency, summary)

    def _calculate(
        self, values: dict, currency: str, summary: str
    ) -> Result:
        requests = values["requests_per_month"]
        current = values["current_input_tokens"]
        output = values["output_tokens"]
        input_price = values["input_price_per_million"]
        output_price = values["output_price_per_million"]

        comparison_supplied = "proposed_input_tokens" in values
        proposed = values.get("proposed_input_tokens", current)

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
            "status": "complete",
            "supplied_inputs": summary,
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

        if currency:
            data["currency"] = currency

        assumptions = (
            "Results use user-supplied figures, which may be estimates or "
            "hypothetical rather than measured usage. Request volume, output "
            "tokens per request and supplied prices remain unchanged. "
            "Both prices must use the same currency. Estimates exclude "
            "caching, discounts, retries, taxes and other fees. "
            "Annualised savings assume unchanged usage for 12 months. "
            "Positive savings mean a reduction; negative savings mean an "
            "increase. Shorter inputs may affect answer quality."
        )

        if not comparison_supplied:
            assumptions += (
                " No proposed input count was supplied, so the comparison "
                "uses the current count and shows no change. To explore "
                "savings, supply a proposed input token count per request."
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
        current_display = data["current_monthly_cost"]
        proposed_display = data["proposed_monthly_cost"]

        if comparison_supplied:
            narrative = (
                f"Estimated monthly cost is {prefix}{current_display:.2f}, "
                f"compared with {prefix}{proposed_display:.2f} "
                "in the proposed scenario."
            )
        else:
            narrative = (
                f"Estimated monthly cost is {prefix}{current_display:.2f}. "
                "What input token count per request would you like to "
                "compare for potential savings?"
            )

        return Result(data=data, narrative=narrative)
