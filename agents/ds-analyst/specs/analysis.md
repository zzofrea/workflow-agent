# DefenderShield Weekly Sales Analysis

## Your Task

Query the DefenderShield ETL database, analyze the prior week's sales data,
and send an HTML email report. The report period is the most recent complete
Monday-through-Sunday week, compared week-over-week to the week before that.

You MUST complete these phases in order:

1. **Query** -- run psql queries to extract all required data
2. **Analyze** -- evaluate findings against significance thresholds
3. **Compose** -- write a narrative summary referencing ONLY queried numbers
4. **Send** -- deliver the HTML email via SMTP

---

## Data Dictionary

Connection: env vars PGHOST, PGPORT, PGUSER, PGDATABASE. Trust auth (no password).

### silver.fact_sales_items (~209k rows)

Individual line items from all sales channels. One row per order-sku-source-modified combination.

- PK: `id` (serial, never query by this)
- Unique: `(order_id, sku, item_source, _modified_date)`
- Join key: `sku` -> `bronze.product_info.sku`

| Column | Type | Description |
|--------|------|-------------|
| order_id | text | Order ID from source system; NOT NULL |
| sku | text | Product SKU; NOT NULL; join to product_info |
| quantity | int | Units sold in this line item; NOT NULL |
| unit_price | numeric(10,2) | USD per unit; imputed via FX for Amazon intl orders |
| line_price | numeric(10,2) | Total line USD = quantity * unit_price |
| marketplace | text | Raw channel -- see normalization rules below |
| status | text | `Completed` / `ReadyToShip` / `Pending` / `Cancelled` |
| sale_date | date | Date the sale occurred |
| shipping_country | text | Destination country (mixed: "US", "United States", country codes) |
| shipping_region | text | State/province name or abbreviation |
| _modified_date | date | Source modification date; drives dedup (latest wins) |
| item_source | text | `FulfilledItems` / `MerchantItems` / `MerchantKits_Bundle` / `MerchantKits_Items` |

Columns you can ignore: `id`, `sale_timestamp`, `part_number`, `shipping_city`, `_created_at`, `raw_unitprice_a`, `raw_lineprice`.

### bronze.product_info (433 rows)

Product catalog from SkuVault. One row per SKU.

- PK: `sku`

| Column | Type | Description |
|--------|------|-------------|
| sku | text | Product SKU; PK |
| classification | text | Category: Apparel/Blanket/General/Glasses/Headphones/Laptops/Miscellaneous/Parts/Phones/Pouch/Privacy & Security/Supplement/Tablets |
| short_description | text | Human-readable product name (use this in reports) |
| is_active | bool | Whether product is currently sold |
| cost | numeric(10,2) | Unit cost (for reference only, not used in analysis) |

All other columns (quantities, supplier info, pricing) are inventory fields -- not relevant to this analysis.

### Gotchas

1. **Dedup is mandatory.** Same (order_id, sku) appears with multiple `_modified_date` values.
   Always use: `ROW_NUMBER() OVER (PARTITION BY order_id, sku ORDER BY _modified_date DESC) AS rn` then `WHERE rn = 1`.
2. **FX gap.** Amazon intl orders have USD prices imputed from foreign currency. ~5% revenue gap vs CSV exports is expected and normal.
3. **Amazon source.** ~96% of Amazon orders use `FulfilledItems`, not `MerchantItems`. Do not filter by item_source.
4. **Pending = $0.** Pending orders often have $0 prices. Always exclude them.
5. **Country inconsistency.** US appears as both `"US"` and `"United States"`. Filter with `IN ('US', 'United States')`.

### Marketplace Normalization

```sql
CASE
  WHEN marketplace IN ('WooCommerce', 'Shopify') THEN 'Website'
  WHEN marketplace IN ('Amazon', 'AmazonUS', 'AmazonCA', 'AmazonAU') THEN 'Amazon'
END AS channel
-- Filter WHERE channel IS NOT NULL (drops Manual, TransferSaleHoldsPendingQuantity)
```

### Status Filter

```sql
WHERE status IN ('Completed', 'ReadyToShip')
-- Excludes Pending ($0 prices) and Cancelled
```

### Required Dedup CTE

Use this in EVERY query:

```sql
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id, sku
    ORDER BY _modified_date DESC
  ) AS rn
  FROM silver.fact_sales_items
  WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT ... FROM deduped WHERE rn = 1
```

### Example Queries

```sql
-- Weekly revenue by channel (use as template)
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id, sku ORDER BY _modified_date DESC) AS rn
  FROM silver.fact_sales_items WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT
  CASE WHEN marketplace IN ('WooCommerce','Shopify') THEN 'Website'
       WHEN marketplace IN ('Amazon','AmazonUS','AmazonCA','AmazonAU') THEN 'Amazon' END AS channel,
  SUM(line_price) AS revenue,
  COUNT(DISTINCT order_id) AS orders
FROM deduped WHERE rn = 1
  AND sale_date BETWEEN '2026-02-16' AND '2026-02-22'
  AND marketplace NOT IN ('Manual','TransferSaleHoldsPendingQuantity')
GROUP BY 1;

-- SKU units with product name
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id, sku ORDER BY _modified_date DESC) AS rn
  FROM silver.fact_sales_items WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT d.sku, p.short_description, SUM(d.quantity) AS units
FROM deduped d JOIN bronze.product_info p ON d.sku = p.sku
WHERE d.rn = 1 AND d.sale_date BETWEEN '2026-02-16' AND '2026-02-22'
  AND d.marketplace NOT IN ('Manual','TransferSaleHoldsPendingQuantity')
GROUP BY d.sku, p.short_description ORDER BY units DESC LIMIT 10;
```

---

## Date Calculations

Determine periods using the current date. Run `date +%Y-%m-%d` to get today,
then calculate:

- **This Week**: The most recent complete Mon-Sun. If today is Monday, that is
  last Mon through yesterday (Sun). If today is Tuesday, go back to prior Mon.
- **Prior Week**: The Mon-Sun immediately before This Week.

Use psql date math:

```sql
-- If today is Monday, this_week_end = yesterday, this_week_start = 7 days ago
-- General formula:
--   this_week_end = today - (extract(isodow from today))::int  -- last Sunday
--   this_week_start = this_week_end - 6                        -- that Monday
--   prior_week_end = this_week_start - 1                       -- prior Sunday
--   prior_week_start = prior_week_end - 6                      -- prior Monday
```

---

## Finding Categories and Thresholds

### 1. Sales Snapshot

Compare total revenue (SUM of line_price) and order count (COUNT DISTINCT
order_id) by channel for This Week vs Prior Week.

**Threshold**: Include this section only if any channel's WoW revenue change
exceeds 10% in either direction.

Output: a table showing channel, this_week_revenue, prior_week_revenue,
wow_change_pct, this_week_orders, prior_week_orders.

### 2. SKU Movers

For each SKU, compare This Week's unit sales to the trailing 4-week average
(the 4 complete weeks ending with Prior Week).

**Thresholds**: Include a SKU only if:
- Velocity deviation > 50% from 4-week average, AND
- Absolute change >= 5 units

**Minimum history**: Exclude SKUs with fewer than 4 weeks of sales history.

Output: top 5 risers and top 5 fallers, showing SKU, product name, this_week
units, 4wk_avg units, deviation_pct.

### 3. Trending Products

Identifies products whose sales momentum shifted this week. Uses 90-day
weekly sales data to detect whether a product is gaining, slowing, or steady.

**Calculation** (internal -- do not expose stats in the email):

1. Aggregate weekly units for each SKU over the past 90 days (~13 data points).
2. Use python3 with scipy.stats.linregress to compute slope, p-value, R-squared.
3. Compute relative slope: `(slope / mean_weekly_units) * 100`.
4. Apply guardrails:
   - Mean weekly volume < 1 unit -> exclude (too low volume to be meaningful)
   - p-value >= 0.10 OR R-squared < 0.15 -> "Steady" (not enough confidence)
5. Classify:
   - Relative slope > +3%/week -> "Gaining"
   - Relative slope < -3%/week -> "Slowing"
   - Otherwise -> "Steady"

**Threshold**: Include this section only if at least one SKU changed direction
compared to the prior week's classification (run the same analysis for the
90-day window ending at prior_week_end and compare).

**Email output**: Label the section "Trending Products". Show a simple table
with columns: Product, Change, Avg Units/Week. The Change column shows the
direction shift in plain English (e.g. "Steady -> Gaining", "Gaining -> Slowing").
Do NOT show slope values, p-values, R-squared, or any statistical terms.

### 4. Geographic Highlights

Compare each state/region's share of total revenue for This Week vs the
trailing 4-week average.

**Threshold**: Include only if a state's share shifted by > 5 percentage points.

Only consider US states (shipping_country = 'US' or 'United States').

Output: states with notable shifts, showing state, this_week_share_pct,
trailing_avg_share_pct, shift_pp.

---

## Email Composition

### Summary

Write the summary as a bulleted list (HTML `<ul>`), maximum 6 bullets.
Each bullet is one key takeaway referencing a specific number from your queries.
Do NOT write a prose paragraph. Use plain business language, no jargon.

Rules for bullets:
- Reference ONLY numbers from your query results
- Do NOT make recommendations ("you should order more")
- Do NOT reference inventory levels
- Do NOT compare to industry benchmarks

### HTML Email Structure

```
Subject: DefenderShield Weekly Brief - {this_week_end date}
  -- OR --
Subject: DefenderShield Weekly Brief - Quiet Week

Body:
  <h2>DefenderShield Weekly Brief</h2>
  <p><em>Week of {this_week_start} to {this_week_end}</em></p>

  <h3>Summary</h3>
  <ul>
    <li>Bullet 1</li>
    <li>Bullet 2</li>
    ...max 6 bullets
  </ul>

  <!-- Only include sections that cleared thresholds -->

  <h3>Sales Snapshot</h3>
  <table>...</table>

  <h3>SKU Movers</h3>
  <table>...</table>

  <h3>Trending Products</h3>
  <table>...</table>

  <h3>Geographic Highlights</h3>
  <table>...</table>
```

For a quiet week (no findings clear any threshold):
```
  <h2>DefenderShield Weekly Brief - Quiet Week</h2>
  <p><em>Week of {this_week_start} to {this_week_end}</em></p>
  <p>Total revenue: ${total_revenue}. Orders: {order_count}.
     No noteworthy changes from the prior week.</p>
```

### Table Styling

Use minimal inline styles for readability:

```html
<table style="border-collapse:collapse;width:100%;font-family:monospace;">
  <tr style="background:#f0f0f0;">
    <th style="text-align:left;padding:4px 8px;border-bottom:2px solid #ccc;">Column</th>
    ...
  </tr>
  <tr>
    <td style="padding:4px 8px;border-bottom:1px solid #eee;">Value</td>
    ...
  </tr>
</table>
```

---

## Email Delivery

Send the email using python3 and smtplib. Read credentials from environment:

```python
import os, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

smtp_host = os.environ["AGENT_SMTP_HOST"]
smtp_port = int(os.environ["AGENT_SMTP_PORT"])
smtp_email = os.environ["AGENT_SMTP_EMAIL"]
smtp_password = os.environ["AGENT_SMTP_PASSWORD"]
recipients = os.environ["AGENT_RECIPIENT_LIST"].split(",")

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"] = smtp_email
msg["To"] = ", ".join(recipients)
msg.attach(MIMEText(html_body, "html"))

with smtplib.SMTP(smtp_host, smtp_port) as server:
    server.starttls()
    server.login(smtp_email, smtp_password)
    server.sendmail(smtp_email, recipients, msg.as_string())
```

---

## Anti-Hallucination Rules

1. Every number you write in the email MUST come directly from a query result
   you ran in this session. Do not round, estimate, or extrapolate.
2. If a query returns no rows, that finding category has no findings. Do not
   invent placeholder data.
3. Do not reference products, SKUs, or regions that did not appear in your
   query results.
4. Do not make forward-looking predictions ("next week will likely...").
5. Do not make recommendations ("consider restocking...", "you should...").
6. Format currency as `$X,XXX.XX`. Format percentages as `XX.X%`.
7. If the database is unreachable, print an error message and stop. Do not
   send any email.

---

## Execution Checklist

Before sending, verify:
- [ ] All queries used the dedup CTE
- [ ] Status filter is Completed + ReadyToShip only
- [ ] Marketplace normalization excludes Manual/transfers
- [ ] Every number in the narrative traces to a query result
- [ ] Email subject follows the specified format
- [ ] HTML renders correctly (close all tags)
- [ ] Recipients come from AGENT_RECIPIENT_LIST env var
