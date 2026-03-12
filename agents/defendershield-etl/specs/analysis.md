# DefenderShield Weekly Sales Analysis

## Your Task

Query the DefenderShield ETL database, analyze the prior week's sales data,
and send an HTML email report. The report period is the most recent complete
Monday-through-Sunday week, compared year-over-year to the same week last year.

You MUST complete these phases in order:

1. **Query** -- run psql queries to extract all required data
2. **Analyze** -- evaluate findings against significance thresholds
3. **Compose** -- write the HTML email and save to `/agent/output/email.html`
4. **Structured Metrics** -- write `/agent/output/metrics.json` from query results
5. **Fact-Check** -- re-run validation SQL, compare to metrics.json
6. **Send** -- deliver the HTML email via SMTP (ONLY if fact-check passed)

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
| created_date_utc | timestamptz | When the product was first added to SkuVault; used to identify new products |
| quantity_available | int | Units currently available to sell across all channels; 0 = out of stock |

All other columns (other quantities, supplier info, pricing) are inventory fields -- not relevant to this analysis.

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

Determine periods using the current date. Run `date +%Y-%m-%d` to get today.

**CRITICAL**: "This Week" means the most recent COMPLETE Monday-through-Sunday
period. It must ALWAYS end on a Sunday and start on the Monday before it.
Today's date is NEVER included unless today is Sunday (in which case today
is the end of the reporting week).

Run this exact psql query to compute all dates -- do NOT calculate by hand:

```sql
SELECT
  current_date AS today,
  current_date - (extract(isodow from current_date))::int AS this_week_end,
  current_date - (extract(isodow from current_date))::int - 6 AS this_week_start,
  current_date - (extract(isodow from current_date))::int - 7 AS prior_week_end,
  current_date - (extract(isodow from current_date))::int - 13 AS prior_week_start,
  current_date - (extract(isodow from current_date))::int - 364 AS yoy_week_end,
  current_date - (extract(isodow from current_date))::int - 370 AS yoy_week_start;
```

Verify the output: this_week_end MUST be a Sunday, this_week_start MUST be a
Monday, and the date range must be exactly 7 days. yoy_week_start MUST be a
Monday and yoy_week_end MUST be a Sunday (52 weeks before the reporting week).
If any of these are wrong, STOP and report the error.

Verify that the YoY period returns data -- if the Sales Snapshot query returns
zero rows for the YoY period, STOP and report an error. Do not send the email.

---

## Finding Categories and Thresholds

### 1. Sales Snapshot

Compare total revenue (SUM of line_price) and order count (COUNT DISTINCT
order_id) by channel for This Week vs Same Week Last Year (YoY).

**Always include this section** -- no threshold gate.

**Email output**: Show a table with these exact headers (no abbreviations):

| Column Header | Description |
|---------------|-------------|
| Channel | "Website" or "Amazon" |
| This Week Revenue | Revenue this week, formatted $X,XXX.XX |
| Last Year Revenue | Revenue same week last year, formatted $X,XXX.XX |
| Revenue Change | YoY percentage change |
| This Week Orders | Order count this week |
| Last Year Orders | Order count same week last year |
| Orders Change | YoY percentage change in orders |

### 2. Biggest Movers

For each SKU, compare This Week's revenue to the trailing 4-week average revenue
(the 4 complete weeks ending with Prior Week).

**Thresholds**: Include a SKU only if:
- Revenue changed more than 50% from the 4-week average, AND
- The absolute revenue change is at least $150

**Minimum history**: Exclude SKUs with fewer than 4 weeks of sales history.

**Email output**: Show top 5 risers and top 5 fallers as separate sub-tables.

**Risers table** headers:

| Column Header | Description |
|---------------|-------------|
| SKU | Product SKU |
| Product Name | `short_description` from product_info |
| This Week Revenue | Revenue this week, formatted $X,XXX.XX |
| 4-Week Average Revenue | Trailing 4-week average revenue, formatted $X,XXX.XX |
| Percent Change | Change vs 4-week average |

**Fallers table** headers (adds Inventory Value column):

| Column Header | Description |
|---------------|-------------|
| SKU | Product SKU |
| Product Name | `short_description` from product_info |
| This Week Revenue | Revenue this week, formatted $X,XXX.XX |
| 4-Week Average Revenue | Trailing 4-week average revenue, formatted $X,XXX.XX |
| Percent Change | Change vs 4-week average |
| In Stock | `quantity_available` from bronze.product_info (integer count) |

**In Stock rules**:
- If `quantity_available` is 0 (out of stock), color the value coral (#FF7043) bold
- Query: JOIN fallers to `bronze.product_info` on SKU for `quantity_available`

### 3. Top States

Compare each state's share of total revenue for This Week vs the
trailing 4-week average.

**Threshold**: Include only if a state's share shifted by more than 5
percentage points.

Only consider US states (shipping_country = 'US' or 'United States').

**Email output**: Show states with notable shifts using these exact headers:

| Column Header | Description |
|---------------|-------------|
| State | US state name |
| This Week Share | Revenue share % this week |
| 4-Week Average Share | Revenue share % trailing 4-week average |
| Change | Difference in percentage points (display as plain number with "pp" suffix in the cell value, NOT in the header) |

**Important**: The "Change" column header must be the single word "Change" --
do not put "(pp)" or any other annotation in the header itself. The unit
"pp" belongs in each cell value (e.g., "+11.1 pp").

### 4. New Product Performance

Tracks products launched within the last 90 days. A product is "new" if its
`created_date_utc` in `bronze.product_info` falls within 90 days of
`this_week_end`.

**Filter**: `is_active = true` AND `created_date_utc >= this_week_end - 89 days`
AND `quantity_available > 0`.
Exclude classifications `'Miscellaneous'` and `'Parts'` (non-sellable SKUs).

**Threshold**: Always include this section if at least one new product exists.
If no new products exist, omit the section entirely.

**Query approach**:

1. Identify all new SKUs from `bronze.product_info` matching the filter above.
2. For each new SKU, query `silver.fact_sales_items` (with dedup CTE) to get:
   - Total units and revenue since the product's `created_date_utc::date`
   - This week's units and revenue
   - Number of distinct weeks with at least one sale (sell-through weeks)
3. Compute derived metrics in the query or post-processing:
   - **Days since launch**: `this_week_end - created_date_utc::date`
   - **Units per week**: total units / (days since launch / 7.0), rounded to 1 decimal
   - **Sell-through rate**: sell-through weeks / total weeks since launch, as a percentage

**Email output**: Label the section "New Product Performance". Show a table:

| Column | Description |
|--------|-------------|
| Product | `short_description` from product_info |
| In Stock | `quantity_available` from product_info |
| Launched | The `created_date_utc` formatted as YYYY-MM-DD |
| Days Live | Days since launch |
| Units (Total) | Total units sold since launch |
| Revenue (Total) | Total revenue since launch, formatted $X,XXX.XX |
| This Week Units | Units sold this week (0 if none) |
| Units/Week | Average units per week since launch |
| Sell-Through | Percentage of weeks with at least one sale |

Sort by `Revenue (Total)` descending.

Color-code the **Sell-Through** column:
- >= 75% -> green `#04BA8D` (strong early traction)
- 25%-74% -> navy `#07043C` (moderate)
- < 25% -> coral `#FF7043` (weak early traction)

Color-code **This Week Units**:
- 0 -> coral `#FF7043`
- > 0 -> default navy `#07043C`

### 5. In Stock - Low Performance

Identifies active, in-stock products with weak or zero sales. This surfaces
SKUs tying up inventory capital without generating revenue.

**Filter**: From `bronze.product_info`, select SKUs where:
- `is_active = true`
- `quantity_available > 0`
- `classification NOT IN ('Miscellaneous', 'Parts')`
- `sku NOT IN ('Misc. - Universal Wallet Case metal adhesives', 'Misc', 'misc. - accessory package', 'total-discount', 'EJ-T48I-OVRN')`
- **Must have at least one historical sale** (EXISTS in deduped fact_sales_items).
  Products that have NEVER sold are not "low performing" and must be excluded.
- **12-month recency floor**: Exclude SKUs whose most recent sale is more than
  12 months before `this_week_end`. Stale inventory that hasn't sold in over a
  year is a catalog/SkuVault cleanup issue, not a weekly performance signal.

**Query approach**:

**CRITICAL**: The final result MUST have exactly one row per SKU. All joins to
fact_sales_items must be pre-aggregated into subqueries grouped by SKU before
joining to product_info. Do NOT join product_info directly to raw or deduped
fact_sales_items rows -- this fans out and creates duplicates.

1. Get the filtered product list with `sku`, `short_description`,
   `quantity_available` from `bronze.product_info`.
2. Inner-join to a subquery (grouped by `sku`) that computes the last sale date
   per SKU: `SELECT sku, MAX(sale_date) AS last_sale FROM deduped WHERE rn = 1 GROUP BY sku`.
   This join inherently excludes SKUs with zero lifetime sales.
   **Also filter**: `last_sale >= this_week_end - INTERVAL '12 months'` to enforce
   the recency floor.
3. Left-join to a second subquery (grouped by `sku`) for the trailing 4-week
   period (`prior_week_start - 21 days` through `this_week_end`):
   `SELECT sku, SUM(quantity) AS units_4wk, SUM(line_price) AS rev_4wk,
   SUM(CASE WHEN sale_date BETWEEN this_week_start AND this_week_end THEN quantity ELSE 0 END) AS units_this_wk
   FROM deduped WHERE rn = 1 AND sale_date BETWEEN trailing_4wk_start AND this_week_end GROUP BY sku`.

Verify the row count matches the number of distinct SKUs before proceeding to
classification.

**Classification tiers** (applied after the query):

- **Zero Sales (4 weeks)**: 0 units sold in the entire trailing 4-week window.
  These are the highest-priority flags.
- **Zero Sales (This Week)**: > 0 units in trailing 4 weeks but 0 units this
  specific week. Only include if trailing 4-week average is also below 2
  units/week (to avoid flagging products with normal weekly variance).
- **Low Velocity**: Sold > 0 units this week but trailing 4-week average is
  below 1 unit/week.

**Threshold**: Include this section if at least one SKU qualifies for any tier.

**Email output**: Label the section "In Stock - Low Performance". Show a table
with products grouped by tier. Within each tier, sort by `quantity_available`
descending (largest idle inventory first).

| Column | Description |
|--------|-------------|
| SKU | The `sku` from product_info (multiple SKUs can share a product name) |
| Product | `short_description` from product_info |
| Status | Tier label: "No Sales (4 wk)", "No Sales (This Wk)", or "Low Velocity" |
| In Stock | `quantity_available` from product_info |
| Last Sale | Most recent sale_date, formatted YYYY-MM-DD |
| 4-Wk Units | Total units sold in trailing 4 weeks |
| 4-Wk Revenue | Total revenue in trailing 4 weeks, formatted $X,XXX.XX |

Cap the table at 8 rows maximum. If more than 8 SKUs qualify, show the top
8 by `quantity_available` descending and add a note below the table:
"Showing top 8 of {N} low-performing in-stock SKUs."

Color-code the **Status** column:
- "No Sales (4 wk)" -> coral `#FF7043` with bold
- "No Sales (This Wk)" -> coral `#FF7043`
- "Low Velocity" -> navy `#07043C`

Color-code the **Last Sale** column:
- Over 30 days ago -> coral `#FF7043`
- 15-30 days ago -> navy `#07043C`
- Under 15 days ago -> default (no special color)

---

## Email Composition

### Brand Identity

DefenderShield brand colors and assets for the email:

- **Primary Blue**: #0553DF
- **Dark Navy**: #07043C (text, headings)
- **Accent Green** (for positive values): #04BA8D
- **Accent Coral** (for negative values): #FF7043
- **Secondary Purple**: #5D53C9
- **Light Blue**: #61B6FC
- **Font**: Archivo (Google Fonts), fallback sans-serif

Color-coding rules -- ONLY percentages get color, everything else is black:
- Positive percentages (e.g. "+14.5%") -> `<span style="color:#04BA8D;font-weight:600;">+14.5%</span>`
- Negative percentages (e.g. "-8.2%") -> `<span style="color:#FF7043;font-weight:600;">-8.2%</span>`
- All other text (names, dollar amounts, unit counts, descriptions) -> dark navy `#07043C`
- New Product Performance: Sell-Through >= 75% -> green, < 25% -> coral
- In Stock - Low Performance: "No Sales" status -> coral, "Low Velocity" -> navy

### Summary

Write the summary as a bulleted list (HTML `<ul>`), maximum 5 bullets.
Each bullet is one punchy takeaway -- short, direct, no filler. All bullet
text is black (#07043C). Only inline percentage values get green/coral color.

**Style rules -- keep bullets tight:**
- Lead with the current number and direction, not the comparison.
  GOOD: "Total revenue $56,769.43 across 575 orders, down -5.9% YoY"
  BAD:  "Total revenue was $56,769.43 this week (-5.9%), on 575 orders, down from $60,341.14 and 590 orders last year."
- Do NOT restate last year's figures -- the tables already show both periods.
  GOOD: "Website revenue dropped -17.3% YoY to $25,975.08"
  BAD:  "Website revenue declined -17.3% year-over-year, from $31,407.55 to $25,975.08"
- One number per bullet. If a bullet has more than two dollar amounts or
  two unit counts, it's too verbose -- split or trim.
- Mover bullets: name + percentage is enough. Skip unit counts.
  GOOD: "Key Fob (ua-fob-bk) led risers at +172.7%"
  BAD:  "Key Fob (ua-fob-bk) led risers at +172.7% vs its trailing 4-week average, with 9 units sold vs an average of 3.3"
- Low performer bullet: just the count.
  GOOD: "11 in-stock SKUs flagged as low performers"
  BAD:  "3 active, in-stock SKUs recorded zero sales over the trailing 4-week window"
  BAD:  "8 in-stock SKUs qualify as low performers across No Sales (4 wk) and No Sales (This Wk) tiers."
  Tier names like "No Sales (4 wk)" are internal labels for the table -- never
  put them in summary bullets. Just state the count.

**Content rules:**
- Reference ONLY numbers from your query results
- Do NOT make recommendations ("you should order more")
- Do NOT reference inventory levels except for the In Stock - Low Performance section
- Do NOT compare to industry benchmarks
- No motivational opener. No exclamation marks. No prose paragraphs.

### HTML Email Structure

```html
<!DOCTYPE html>
<html>
<head>
  <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:'Archivo',sans-serif;color:#07043C;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;margin-top:20px;margin-bottom:20px;">

    <!-- Header -->
    <div style="background:#07043C;padding:24px 32px;text-align:left;">
      <img src="https://defendershield.com/cdn/shop/files/DS-Logo-M.svg?v=1743535191&width=600"
           alt="DefenderShield"
           style="max-width:220px;height:auto;display:block;margin-bottom:12px;"
           onerror="this.style.display='none';this.nextElementSibling.style.marginTop='0';" />
      <p style="color:#ffffff;font-family:'Archivo',sans-serif;font-size:14px;font-weight:400;margin:0;letter-spacing:0.5px;">
        Weekly Sales Brief &mdash; {this_week_start} to {this_week_end}
      </p>
    </div>

    <!-- Body -->
    <div style="padding:24px 32px;">

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        Summary
      </h3>
      <ul style="padding-left:20px;line-height:1.8;">
        <!-- Color-coded bullets here -->
      </ul>

      <!-- Only include sections that cleared thresholds -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        Sales Snapshot
      </h3>
      <!-- table -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        Biggest Movers
      </h3>
      <!-- table -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        Top States
      </h3>
      <!-- table -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        New Product Performance
      </h3>
      <!-- table -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        In Stock - Low Performance
      </h3>
      <!-- table -->

    </div>

    <!-- Footer -->
    <div style="background:#f4f5f7;padding:16px 32px;text-align:center;border-top:1px solid #e0e0e0;">
      <span style="font-size:13px;font-weight:600;color:#F07628;">Rusty Data</span>
      <p style="color:#999;font-size:11px;margin:0;">
        Automated analysis by Rusty Data | Powered by Claude
      </p>
    </div>

  </div>
</body>
</html>
```

**Logo note**: The header `<img>` tag loads the DefenderShield logo from their
Shopify CDN. The `onerror` fallback renders the text "DefenderShield" if the
image fails to load. Do NOT replace this with a base64 string.


For a quiet week (no findings clear any threshold), use the same template
structure but replace the body content with:
```html
<p style="color:#07043C;">
  Total revenue: ${total_revenue}. Orders: {order_count}.
  No noteworthy year-over-year changes.
</p>
```

Subject line: `DefenderShield Weekly Brief - {this_week_end}` or
`DefenderShield Weekly Brief - Quiet Week`

### Table Header Rules

All table column headers MUST use full, unabbreviated words exactly as
specified in each section's column table above. Never abbreviate headers
(e.g., use "This Week Revenue" not "TW Rev", use "Orders Change" not
"Ord Change", use "Percent Change" not "% Chg"). Units and annotations
belong in cell values, not in headers.

### Table Styling

Use DefenderShield brand styling for all data tables:

```html
<table style="border-collapse:collapse;width:100%;font-family:'Archivo',sans-serif;font-size:13px;">
  <tr style="background:#07043C;color:#ffffff;">
    <th style="text-align:left;padding:8px 12px;font-weight:600;">Column</th>
    ...
  </tr>
  <tr style="background:#ffffff;">
    <td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;">Value</td>
    ...
  </tr>
  <tr style="background:#f4f5f7;">
    <td style="padding:8px 12px;border-bottom:1px solid #e0e0e0;">Value</td>
    ...
  </tr>
</table>
```

Color-code change values in table cells:
- Positive: `<span style="color:#04BA8D;font-weight:600;">+14.5%</span>`
- Negative: `<span style="color:#FF7043;font-weight:600;">-8.2%</span>`
- Neutral: plain `#07043C`

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

## Phase 3 Addendum: Save Email HTML

After composing the HTML email, you MUST write it to `/agent/output/email.html`
before proceeding to the next phase. This preserves the email regardless of
whether the fact-check passes or fails.

```bash
cat > /agent/output/email.html << 'EMAIL_EOF'
{paste the complete HTML email here}
EMAIL_EOF
```

---

## Phase 4: Structured Metrics

Immediately after saving the email HTML, write `/agent/output/metrics.json`
containing structured figures from the SAME query results you used to compose
the email. Use this exact schema:

```json
{
  "this_week_revenue": ...,
  "yoy_week_revenue": ...,
  "yoy_change_pct": ...,
  "channel_breakdown": {
    "Website": {"this_week": ..., "last_year": ...},
    "Amazon": {"this_week": ..., "last_year": ...}
  },
  "this_week_orders": ...,
  "yoy_week_orders": ...,
  "movers_risers_count": ...,
  "movers_fallers_count": ...,
  "movers_detail": [
    {"sku": "...", "this_week_revenue": ..., "avg_revenue": ..., "pct_change": ..., "in_stock": ...}
  ],
  "low_performers_count": ...,
  "fact_check_passed": null
}
```

The `movers_detail` array MUST contain every SKU that appears in the Biggest
Movers section of the email (both risers and fallers). Each entry records the
exact values used to generate the email table row:
- `sku`: the product SKU string
- `this_week_revenue`: revenue this week (numeric, 2 decimal)
- `avg_revenue`: trailing 4-week average revenue (numeric, 2 decimal)
- `pct_change`: percent change vs 4-week average (numeric, 1 decimal)
- `in_stock`: optional, included for faller entries only (`quantity_available` from product_info, integer; omitted for risers)

All numeric values must be the exact figures from your query results -- do NOT
round, estimate, or recalculate. The `fact_check_passed` field starts as `null`
and is updated after the fact-check phase.

Write it using this Bash command:

```bash
cat > /agent/output/metrics.json << 'METRICS_EOF'
{paste the JSON here with actual values}
METRICS_EOF
```

---

## Phase 5: Fact-Check

Run these EXACT SQL queries (substituting the date variables you computed in
Phase 1) and compare results against metrics.json. Do NOT modify these queries.

### FC-1: Total revenue and orders by period

```sql
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id, sku ORDER BY _modified_date DESC
  ) AS rn
  FROM silver.fact_sales_items
  WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT
  CASE
    WHEN sale_date BETWEEN '{this_week_start}' AND '{this_week_end}' THEN 'this_week'
    WHEN sale_date BETWEEN '{yoy_week_start}' AND '{yoy_week_end}' THEN 'yoy_week'
  END AS period,
  ROUND(SUM(line_price)::numeric, 2) AS total_revenue,
  COUNT(DISTINCT order_id) AS orders
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN '{yoy_week_start}' AND '{this_week_end}'
  AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
GROUP BY 1 ORDER BY 1;
```

### FC-2: Revenue by channel by period

```sql
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id, sku ORDER BY _modified_date DESC
  ) AS rn
  FROM silver.fact_sales_items
  WHERE status IN ('Completed', 'ReadyToShip')
)
SELECT
  CASE
    WHEN sale_date BETWEEN '{this_week_start}' AND '{this_week_end}' THEN 'this_week'
    WHEN sale_date BETWEEN '{yoy_week_start}' AND '{yoy_week_end}' THEN 'yoy_week'
  END AS period,
  CASE
    WHEN marketplace IN ('WooCommerce', 'Shopify') THEN 'Website'
    WHEN marketplace IN ('Amazon', 'AmazonUS', 'AmazonCA', 'AmazonAU') THEN 'Amazon'
  END AS channel,
  ROUND(SUM(line_price)::numeric, 2) AS revenue
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN '{yoy_week_start}' AND '{this_week_end}'
  AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
GROUP BY 1, 2 ORDER BY 1, 2;
```

### FC-3: Biggest Movers per-SKU validation

This query re-derives the movers using the same logic as the analysis phase.
Compare each SKU in `movers_detail` against the FC-3 results.

```sql
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id, sku ORDER BY _modified_date DESC
  ) AS rn
  FROM silver.fact_sales_items
  WHERE status IN ('Completed', 'ReadyToShip')
),
this_week AS (
  SELECT sku, ROUND(SUM(line_price)::numeric, 2) AS tw_revenue
  FROM deduped
  WHERE rn = 1
    AND sale_date BETWEEN '{this_week_start}' AND '{this_week_end}'
    AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
  GROUP BY sku
),
trail_avg AS (
  SELECT sku, ROUND((SUM(line_price) / 4.0)::numeric, 2) AS avg_revenue
  FROM deduped
  WHERE rn = 1
    AND sale_date BETWEEN '{prior_week_start}'::date - 21 AND '{prior_week_end}'
    AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
  GROUP BY sku
  HAVING COUNT(DISTINCT date_trunc('week', sale_date)) >= 4
)
SELECT
  COALESCE(t.sku, tr.sku) AS sku,
  COALESCE(t.tw_revenue, 0) AS this_week_revenue,
  tr.avg_revenue,
  ROUND(((COALESCE(t.tw_revenue, 0) - tr.avg_revenue) / tr.avg_revenue * 100)::numeric, 1) AS pct_change
FROM trail_avg tr
LEFT JOIN this_week t ON t.sku = tr.sku
WHERE tr.avg_revenue > 0
  AND ABS(COALESCE(t.tw_revenue, 0) - tr.avg_revenue) >= 150
  AND ABS((COALESCE(t.tw_revenue, 0) - tr.avg_revenue) / tr.avg_revenue * 100) > 50
ORDER BY pct_change DESC;
```

### Comparison Rules

For each metric, compute: `abs(metrics_value - fc_value) / fc_value`

If this ratio exceeds 0.005 (0.5%) for ANY metric, the fact-check FAILS.

Metrics to validate (all checks must pass):

| FC Query | FC Row | Metric in metrics.json |
|----------|--------|----------------------|
| FC-1 | this_week / total_revenue | `this_week_revenue` |
| FC-1 | this_week / orders | `this_week_orders` |
| FC-1 | yoy_week / total_revenue | `yoy_week_revenue` |
| FC-1 | yoy_week / orders | `yoy_week_orders` |
| FC-2 | this_week / Website | `channel_breakdown.Website.this_week` |
| FC-2 | this_week / Amazon | `channel_breakdown.Amazon.this_week` |
| FC-2 | yoy_week / Website | `channel_breakdown.Website.last_year` |
| FC-2 | yoy_week / Amazon | `channel_breakdown.Amazon.last_year` |
| FC-3 | per-SKU | `movers_detail` (see below) |

**FC-3 per-SKU validation**:

For each entry in `movers_detail`, find the matching SKU row in the FC-3
query results. The check FAILS if:
- A SKU in `movers_detail` does not appear in the FC-3 results (phantom SKU)
- A SKU in `movers_detail` has a `pct_change` that differs from the FC-3
  `pct_change` by more than 1.0 percentage point absolute
  (e.g., metrics says +176.9%, FC-3 says +176.0% -> pass;
   metrics says -100.0%, FC-3 says -58.8% -> fail)
- A SKU in `movers_detail` has `this_week_revenue` that differs from FC-3

When reporting FC-3 discrepancies, list each failing SKU separately with
the metric name format `movers_detail.<sku>.this_week_revenue`.

After comparison, update metrics.json:
- If ALL checks pass: set `fact_check_passed` to `true`
- If ANY check fails: set `fact_check_passed` to `false` and add a
  `"discrepancies"` array listing each failed check:

```json
{
  "discrepancies": [
    {
      "metric": "this_week_revenue",
      "metrics_value": 50415.90,
      "fc_value": 56769.43,
      "pct_diff": 11.2
    }
  ]
}
```

---

## Phase 6: Send (Conditional)

**This replaces the unconditional send.** Check the fact-check result before
sending:

```
IF fact_check_passed is true:
  Read the email HTML from /agent/output/email.html
  Send via SMTP using the existing delivery code below
ELSE:
  Do NOT send the email
  Print: "FACT-CHECK FAILED: Email saved to /agent/output/email.html but not sent."
  Print each discrepancy from metrics.json
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
