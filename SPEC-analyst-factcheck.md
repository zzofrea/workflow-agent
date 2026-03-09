# SPEC: Analyst Structured Metrics & Fact-Check Gate

## System Overview

Add a structured metrics output and fact-check validation phase to the
DefenderShield analyst agent. The agent currently sends an HTML email with
query-derived numbers, then writes a free-text summary to report.json that
frequently hallucinates figures. This change gates email delivery on a
mechanical fact-check: the agent writes structured metrics from its analysis
queries, re-runs explicit validation SQL, and only sends the email if all
numbers match within tolerance. If validation fails, the email HTML is saved
but not sent, and the report surfaces the discrepancies.

## Behavioral Contract

### Happy Path

- When the analyst completes its analysis queries, it writes a `metrics.json`
  file to `/agent/output/` containing structured revenue and channel figures.
- When the agent runs the fact-check queries and all values match `metrics.json`
  within 0.5% relative tolerance, the agent sends the email and sets overall to
  "complete".
- When the entrypoint finds a `metrics.json` alongside the report, it merges
  the structured metrics into `report.json` under a `metrics` key.

### Error Flows

- When any fact-check value exceeds 0.5% tolerance, the agent writes the
  composed email HTML to `/agent/output/email.html` but does NOT send it.
  The agent sets overall to "error" with a summary listing each discrepancy.
- When a fact-check query fails (connection error, syntax error, timeout), the
  agent does NOT send the email. Overall is "error" with the failure reason.
- When the agent times out or hits max turns before completing the fact-check,
  the entrypoint produces an "incomplete" report. No email is sent (the agent
  never reached the send phase).

### Boundary Conditions

- When `metrics.json` is missing (agent crashed before writing it), the
  entrypoint treats the run as "incomplete" -- no metrics are merged.
- When `metrics.json` exists but is malformed JSON, the entrypoint logs a
  warning and treats the run as "error".

## Explicit Non-Behaviors

- The system must not change the email HTML composition, layout, styling, or
  summary bullet logic, because those are working correctly.
- The system must not change the analyst role YAML (model, timeout, max_turns),
  because the current settings have sufficient headroom.
- The system must not add retry logic for failed fact-checks, because a
  mismatch indicates a real problem that should be investigated, not retried.
- The system must not create new utility modules or abstractions for the
  metrics merge -- the logic is a single if-block in the entrypoint.

## Integration Boundaries

### 1. Analysis Spec (analysis.md)

- **What changes**: Two new phases added (Structured Metrics + Fact-Check),
  existing Phase 4 (Send) reordered to Phase 6 and gated on fact-check pass.
- **Data flow**: Agent writes `metrics.json` to `/agent/output/` via Bash
  tool. Agent runs fact-check SQL via psql. Agent compares results.
- **Failure mode**: If the agent cannot write `metrics.json` (disk full,
  permissions), the email is not sent and the run is incomplete.

### 2. Entrypoint (entrypoint.py)

- **What changes**: `_build_markdown_output()` checks for
  `/agent/output/metrics.json` and merges it into the report dict.
- **Data flow**: Reads `metrics.json` from disk, parses as JSON, adds as
  `report["metrics"]` key. If `metrics.json` contains
  `"fact_check_passed": false`, overrides overall to "error".
- **Failure mode**: Missing file = no merge (backward compatible). Malformed
  JSON = log warning, set overall to "error".
- **Security**: No secrets involved. File is read from a controlled container
  volume.

### 3. Notification Routing (output.py -- NO CHANGES)

- Existing routing handles overall="error" by sending to agent-logs channel
  with severity "warning". No changes needed.

## Behavioral Scenarios

### Happy Path

**Scenario 1: Analyst sends email after passing fact-check**

```
GIVEN the ETL database contains completed sales data for the reporting week
  AND the analyst agent runs its analysis queries and composes an email
WHEN the agent writes metrics.json with revenue totals and channel breakdown
  AND the agent runs fact-check queries that match within 0.5% tolerance
THEN the agent sends the email to the configured recipients
  AND report.json contains overall "complete"
  AND report.json contains a "metrics" key with structured revenue figures
  AND the metrics values match the database within 0.5%
```

**Scenario 2: Structured metrics are present in archived report**

```
GIVEN the analyst agent completed successfully
WHEN the report is archived to ~/agent-output/
THEN report.json contains a "metrics" object with at minimum:
  - this_week_revenue (number)
  - prior_week_revenue (number)
  - wow_change_pct (number)
  - channel_breakdown (object with Website and Amazon keys, each having
    this_week and prior_week numbers)
```

**Scenario 3: Fact-check tolerates minor floating-point drift**

```
GIVEN the analysis query computes this_week_revenue as $56,769.43
  AND the fact-check query computes this_week_revenue as $56,769.44
WHEN the agent compares the two values
THEN the fact-check passes (difference is <0.5%)
  AND the email is sent normally
```

### Error Flows

**Scenario 4: Fact-check blocks email on revenue mismatch**

```
GIVEN the analysis query computes this_week_revenue as $50,415.90
  AND the fact-check query computes this_week_revenue as $56,769.43
WHEN the agent compares the two values
THEN the difference exceeds 0.5% tolerance
  AND the agent writes the email HTML to /agent/output/email.html
  AND the agent does NOT send the email
  AND report.json contains overall "error"
  AND report.json contains the specific discrepancy details
  AND a Discord notification is sent to agent-logs channel
```

**Scenario 5: Fact-check query failure blocks email**

```
GIVEN the analyst agent has composed the email and written metrics.json
WHEN a fact-check query fails due to a database connection error
THEN the agent does NOT send the email
  AND report.json contains overall "error"
  AND the error reason references the database failure
  AND a Discord notification is sent to agent-logs channel
```

### Edge Cases

**Scenario 6: Agent times out before completing fact-check**

```
GIVEN the analyst agent has sent 40 turns (max_turns limit)
  AND the agent has not yet completed the fact-check phase
WHEN the Claude CLI exits due to turn limit
THEN report.json contains overall "incomplete"
  AND no email was sent (send phase was never reached)
  AND a Discord notification is sent to agent-logs channel
```

**Scenario 7: Partial fact-check failure blocks email**

```
GIVEN the total revenue fact-check passes within tolerance
  AND the Website channel revenue fact-check exceeds 0.5% tolerance
WHEN the agent evaluates the overall fact-check result
THEN the fact-check is considered FAILED (all checks must pass)
  AND the email is NOT sent
  AND report.json lists the specific channel discrepancy
```

## Changes Required

### 1. analysis.md -- Add Phases 5-6, Reorder Send to Phase 6

After existing Phase 3 (Compose), add:

**Phase 4 -- Structured Metrics**

Instruct the agent to write `/agent/output/metrics.json` with this exact
schema immediately after composing the email (before sending):

```json
{
  "this_week_revenue": 56769.43,
  "prior_week_revenue": 60341.14,
  "wow_change_pct": -5.9,
  "channel_breakdown": {
    "Website": {"this_week": 25975.08, "prior_week": 31407.55},
    "Amazon": {"this_week": 30794.35, "prior_week": 28933.59}
  },
  "this_week_orders": 575,
  "prior_week_orders": 590,
  "movers_risers_count": 15,
  "movers_fallers_count": 3,
  "low_performers_count": 7,
  "fact_check_passed": null
}
```

The agent must populate these values from the SAME query results it used to
compose the email. The `fact_check_passed` field starts as `null` and is
updated after the fact-check phase.

Provide this exact Bash command template for writing the file:

```bash
cat > /agent/output/metrics.json << 'METRICS_EOF'
{paste the JSON here}
METRICS_EOF
```

**Phase 5 -- Fact-Check**

Provide these EXACT SQL queries in the spec. The agent must run each one
verbatim (not interpret or modify them) and compare results against
`metrics.json`:

```sql
-- FC-1: Total revenue by period
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id, sku ORDER BY _modified_date DESC
  ) AS rn
  FROM silver.fact_sales_items
  WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT
  CASE
    WHEN sale_date BETWEEN {this_week_start} AND {this_week_end} THEN 'this_week'
    WHEN sale_date BETWEEN {prior_week_start} AND {prior_week_end} THEN 'prior_week'
  END AS period,
  ROUND(SUM(line_price)::numeric, 2) AS total_revenue,
  COUNT(DISTINCT order_id) AS orders
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN {prior_week_start} AND {this_week_end}
  AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
GROUP BY 1 ORDER BY 1;
```

```sql
-- FC-2: Revenue by channel
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id, sku ORDER BY _modified_date DESC
  ) AS rn
  FROM silver.fact_sales_items
  WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT
  CASE
    WHEN sale_date BETWEEN {this_week_start} AND {this_week_end} THEN 'this_week'
    WHEN sale_date BETWEEN {prior_week_start} AND {prior_week_end} THEN 'prior_week'
  END AS period,
  CASE
    WHEN marketplace IN ('WooCommerce', 'Shopify') THEN 'Website'
    WHEN marketplace IN ('Amazon', 'AmazonUS', 'AmazonCA', 'AmazonAU') THEN 'Amazon'
  END AS channel,
  ROUND(SUM(line_price)::numeric, 2) AS revenue
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN {prior_week_start} AND {this_week_end}
  AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
GROUP BY 1, 2 ORDER BY 1, 2;
```

The agent substitutes the date variables it already computed in Phase 1.

**Fact-check comparison rules** (include verbatim in the spec):

```
For each metric, compute: abs(metrics_value - fc_value) / fc_value
If this ratio exceeds 0.005 (0.5%) for ANY metric, the fact-check FAILS.

Metrics to validate:
  FC-1 this_week row  -> metrics.this_week_revenue, metrics.this_week_orders
  FC-1 prior_week row -> metrics.prior_week_revenue, metrics.prior_week_orders
  FC-2 this_week/Website -> metrics.channel_breakdown.Website.this_week
  FC-2 this_week/Amazon  -> metrics.channel_breakdown.Amazon.this_week
  FC-2 prior_week/Website -> metrics.channel_breakdown.Website.prior_week
  FC-2 prior_week/Amazon  -> metrics.channel_breakdown.Amazon.prior_week

After comparison, update metrics.json:
  - Set fact_check_passed to true or false
  - If false, add a "discrepancies" array listing each failed check with
    metric name, metrics_value, fc_value, and pct_diff
```

**Phase 3 update -- Compose (always write to file)**

After composing the HTML email, the agent MUST write it to
`/agent/output/email.html` before proceeding. This preserves the email
regardless of whether the fact-check passes or fails.

**Phase 6 -- Send (conditional)**

Move the existing email send logic here. Gate it:

```
IF fact_check_passed is true:
  Read the email HTML from /agent/output/email.html
  Send via SMTP (existing logic, unchanged)
ELSE:
  Do NOT send
  Print: "FACT-CHECK FAILED: Email saved to /agent/output/email.html but not sent."
  Print each discrepancy.
```

### 2. entrypoint.py -- Merge metrics.json into report

In `_build_markdown_output()`, after building the base report dict, add logic
to check for `/agent/output/metrics.json`:

```python
metrics_path = os.path.join(OUTPUT_DIR, "metrics.json")
if os.path.exists(metrics_path):
    try:
        with open(metrics_path) as f:
            metrics = json.load(f)
        report["metrics"] = metrics
        if metrics.get("fact_check_passed") is False:
            report["overall"] = "error"
            report["summary"] = (
                f"Fact-check failed: {len(metrics.get('discrepancies', []))} "
                f"discrepancy(ies) found. Email not sent."
            )
    except (json.JSONDecodeError, OSError) as exc:
        report["overall"] = "error"
        report["summary"] = f"Failed to read metrics.json: {exc}"
```

This is backward compatible: if no `metrics.json` exists (e.g., auditor role),
the report is unchanged.

## Definition of Done

- [ ] analysis.md updated with Phases 4-6 (metrics, fact-check, conditional send)
- [ ] entrypoint.py merges metrics.json into report when present
- [ ] entrypoint.py overrides overall to "error" when fact_check_passed is false
- [ ] Existing entrypoint tests still pass
- [ ] New unit tests cover: metrics merge happy path, missing metrics.json,
      malformed metrics.json, fact_check_passed=false override
- [ ] Manual validation: run analyst, confirm metrics.json in archive,
      confirm report.json has structured metrics key
- [ ] Notification routing confirmed: fact-check failure -> agent-logs Discord

## PROJECT.md

```markdown
# Analyst Fact-Check Gate

## Objective
Prevent the analyst agent from sending emails with hallucinated summary
numbers by adding structured metrics output and a mechanical fact-check
phase that gates email delivery.

## Acceptance Criteria
- Analyst writes metrics.json with structured revenue/channel figures
- Fact-check re-runs key SQL and compares against metrics.json
- Email only sent if all metrics match within 0.5% tolerance
- Failed fact-check saves email HTML but does not send
- report.json contains structured metrics for downstream consumers

## Constraints
- Email composition, layout, and styling must not change
- Analyst role YAML (model, timeout, max_turns) must not change
- Fact-check SQL must be explicit in the spec, not agent-interpreted
- Entrypoint changes must be backward compatible (no metrics.json = no change)

## What "Done" Means
- Analyst runs end-to-end with fact-check passing
- Fact-check failure correctly blocks email and alerts via Discord
- All existing tests pass, new tests cover metrics merge logic

## Out of Scope
- Changing the auditor or any other role
- Adding retry logic for failed fact-checks
- Modifying notification routing
```

## Ambiguity Warnings

1. **WoW percentage validation**: The spec validates revenue totals and channel
   splits but not the computed WoW percentage itself. The percentage is derived
   from the validated numbers, so it should be correct if the inputs are correct.
   However, a rounding difference in the percentage display is possible.
   **Assumption**: We do not separately validate WoW % since it's derived.
   **Resolution needed if**: You want the percentage independently checked.

2. **Movers/low-performers counts**: RESOLVED -- FC-3 now validates per-SKU
   movers data. `movers_detail` in metrics.json records each SKU's units and
   percent change; FC-3 re-derives the same query and compares. A phantom SKU
   or a pct_change deviation >1.0pp triggers a fact-check failure.

3. **Email HTML preservation on failure**: RESOLVED -- the agent always writes
   the composed email HTML to `/agent/output/email.html` in Phase 3. Phase 6
   either sends it (fact-check passed) or leaves it for inspection (failed).
   No dependency on the agent holding HTML in memory across phases.

## Implementation Constraints

- Language: Python 3.11+ (matches existing entrypoint)
- Changes limited to: `analysis.md` (spec), `entrypoint.py` (metrics merge),
  `tests/test_entrypoint.py` (new test cases)
- No new dependencies

## Constitution Compliance

| Rule | Satisfaction |
|------|-------------|
| 1-6 (Specs before code, Given/When/Then) | Behavioral scenarios written in Given/When/Then format above |
| 7 (Human approval before implementation) | This spec requires approval via /spec workflow |
| 8-10 (Two test streams, red-green) | Unit tests for entrypoint merge + manual acceptance validation defined |
| 11-16 (Code quality) | Implementation constrained to minimal changes with type hints |
| 17-19 (Security) | No secrets involved; file I/O within controlled container volume |
| 20-21 (Docker/Dokploy) | No deployment changes; runs in existing agent container |
| 22 (Post-merge hooks) | No new dependencies to cascade |
| 23 (PROJECT.md) | Template provided above |
| 24-27 (Definition of done) | Checklist provided with tests, deployment, monitoring |
| 28-31 (Simplicity) | No new abstractions; metrics merge is a single if-block; no unused code |
