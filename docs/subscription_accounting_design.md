# Subscription accounting design note

Tracking: `mu-mucm.7.4`.

The dashboard currently reports subscription traffic as API-rate-equivalent cost.
That is useful for comparing harness/model usage, but it is not actual charged
spend. Actual subscription accounting needs an explicit product choice before it
is wired into the dashboard.

## Decision needed

Pick the primary display semantics for subscription spend:

1. **Calendar-window actual spend**
   - A plan has a start/end date and a charged amount.
   - Dashboard reports the full charged amount in the configured billing window.
   - Best answer for: “What did I pay this month?”
   - Bad answer for: short-range charts, because cost appears as a lump/window.

2. **Rolling amortized spend**
   - A plan has a cost and service interval.
   - Dashboard spreads the actual cost over active days/hours.
   - Best answer for: “What is this dashboard period’s share of subscription cost?”
   - Bad answer for: bank-statement reconciliation unless paired with calendar spend.

Recommendation: show **both**, but name one primary:

- Primary KPI: `actual_subscription_spend_calendar`
- Secondary/comparison KPI: `actual_subscription_spend_amortized`
- Keep existing `total_api_rate_equiv` as a separate utilization metric, never as
  actual spend.

## Proposed config shape

```toml
[[subscription_plans]]
provider = "anthropic"
account = "personal"
label = "Claude Max"
starts_on = "2026-06-01"
ends_on = "2026-07-01"
cost_usd = 200.00
fleets = ["mu", "cc"]
models = ["claude-"]
allocation = "all_matching_usage" # or "manual"
```

Optional future fields:

```toml
actual_charge_date = "2026-06-01"
currency = "USD"
notes = "receipt/order id, if useful"
manual_allocation_usd = 123.45
```

## Dashboard contract sketch

```json
{
  "subscription_accounting": {
    "calendar_spend_usd": 200.0,
    "amortized_spend_usd": 109.68,
    "api_rate_equiv_usd": 12429.0,
    "plans": [
      {
        "label": "Claude Max",
        "provider": "anthropic",
        "account": "personal",
        "starts_on": "2026-06-01",
        "ends_on": "2026-07-01",
        "cost_usd": 200.0,
        "active_days_in_dashboard_window": 17,
        "total_plan_days": 30,
        "amortized_spend_usd": 113.33,
        "api_rate_equiv_usd": 10000.0
      }
    ]
  }
}
```

## UI rules

- Never label API-rate-equivalent subscription traffic as “actual spend”.
- Use wording like:
  - `API-rate equivalent` for utilization/comparison.
  - `Actual subscription spend` for configured plan charges.
  - `Amortized actual spend` for windowed allocation.
- If no subscription config exists, keep current API-rate-equivalent view and show
  a small caveat: “actual plan spend not configured”.
- If configured windows do not cover visible usage, warn rather than silently
  dropping spend.

## Non-goals for the first implementation

- No provider billing API integration.
- No receipt scraping.
- No automatic account detection.
- No attempt to infer plan price from model names.
- No merging API-rate-equivalent and actual spend into one total.

## Suggested first PR after decision

1. Add config parsing and validation for `[[subscription_plans]]`.
2. Add a pure function that computes calendar and amortized spend for a dashboard
   date window.
3. Add tests for overlapping, missing, and partial windows.
4. Add a small Cost page card that displays actual subscription spend only when
   configured, while preserving API-rate-equivalent as separate.
