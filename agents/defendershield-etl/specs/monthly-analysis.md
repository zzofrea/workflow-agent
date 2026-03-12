# DefenderShield Monthly Sales Analysis

## Your Task

Query the DefenderShield ETL database, analyze the prior month's sales data,
and send an HTML email report. The report period is the most recent complete
calendar month, compared year-over-year to the same month last year.

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
-- Monthly revenue by channel (use as template)
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
  AND sale_date BETWEEN '2026-02-01' AND '2026-02-28'
  AND marketplace NOT IN ('Manual','TransferSaleHoldsPendingQuantity')
GROUP BY 1;
```

---

## Date Calculations

Determine periods using the current date. Run `date +%Y-%m-%d` to get today.

**CRITICAL**: "This Month" means the most recent COMPLETE calendar month.
If today is March 11, the report covers February 1-28 (or Feb 1-29 in leap
years). Today's month is NEVER included.

Run this exact psql query to compute all dates -- do NOT calculate by hand:

```sql
SELECT
  current_date AS today,
  date_trunc('month', current_date) - interval '1 day' AS report_month_end,
  date_trunc('month', current_date) - interval '1 month' AS report_month_start,
  to_char(date_trunc('month', current_date) - interval '1 month', 'FMMonth') AS report_month_name,
  extract(year from date_trunc('month', current_date) - interval '1 month')::int AS report_year,
  (date_trunc('month', current_date) - interval '1 month') - interval '1 year' AS yoy_month_start,
  (date_trunc('month', current_date) - interval '1 month') - interval '1 year' + interval '1 month' - interval '1 day' AS yoy_month_end,
  (date_trunc('month', current_date) - interval '1 month') - interval '3 months' AS trailing_3mo_start,
  date_trunc('month', current_date) - interval '1 month' - interval '1 day' AS trailing_3mo_end,
  date_trunc('year', date_trunc('month', current_date) - interval '1 month') AS ytd_start,
  date_trunc('month', current_date) - interval '1 day' AS ytd_end,
  date_trunc('year', date_trunc('month', current_date) - interval '1 month') - interval '1 year' AS prev_ytd_start,
  (date_trunc('month', current_date) - interval '1 month') - interval '1 year' + interval '1 month' - interval '1 day' AS prev_ytd_end;
```

Verify the output:
- `report_month_start` MUST be the 1st of the prior month
- `report_month_end` MUST be the last day of the prior month
- `yoy_month_start` MUST be the 1st of the same month last year
- `yoy_month_end` MUST be the last day of the same month last year
- `trailing_3mo_start` MUST be the 1st of the month 3 calendar months before
  the report month (e.g., for Feb report: Nov 1)
- `trailing_3mo_end` MUST be the last day of the month before the report month
  (same as report_month_end - this is the full trailing window)
- `ytd_start` MUST be January 1 of the report year
- `ytd_end` MUST equal `report_month_end`
- `prev_ytd_start` MUST be January 1 of the year before the report year
- `prev_ytd_end` MUST equal `yoy_month_end`

Note: For January reports, YTD equals the monthly figures (single-month window).
This is correct behavior.

If any of these are wrong, STOP and report the error.

Also compute: `lifecycle_start = report_month_end::date - 89` (for new product filter).

Verify that the YoY period returns data -- if the Sales Snapshot query returns
zero rows for the YoY period, STOP and report an error. Do not send the email.

---

## Finding Categories and Thresholds

### 1. Sales Snapshot

Compare total revenue (SUM of line_price) and order count (COUNT DISTINCT
order_id) by channel for This Month vs Same Month Last Year (YoY).

**Always include this section** -- no threshold gate.

**Email output**: Show a table with these exact headers (no abbreviations):

| Column Header | Description |
|---------------|-------------|
| Channel | "Website" or "Amazon" |
| This Month Revenue | Revenue this month, formatted $X,XXX.XX |
| Same Month Last Year | Revenue same month last year, formatted $X,XXX.XX |
| Revenue Change | YoY percentage change |
| This Month Orders | Order count this month |
| Last Year Orders | Order count same month last year |
| Orders Change | YoY percentage change in orders |
| Year-to-Date Sales | Revenue from ytd_start through ytd_end per channel, formatted $X,XXX.XX |
| Previous Year Year-to-Date Sales | Revenue from prev_ytd_start through prev_ytd_end per channel, formatted $X,XXX.XX |

**YTD query guidance**: Compute per-channel YTD by running a separate query (or
extending Q1) with `sale_date BETWEEN '{ytd_start}' AND '{ytd_end}'` for current
YTD and `sale_date BETWEEN '{prev_ytd_start}' AND '{prev_ytd_end}'` for previous
year YTD, grouped by channel using the same marketplace normalization.

### 2. Biggest Movers

For each SKU, compare This Month's revenue to the Same Month Last Year's
revenue.

**Thresholds**: Include a SKU only if:
- The SKU had >$0 revenue in BOTH this month AND the same month last year
- Revenue changed more than 50% from the same month last year, AND
- The absolute revenue change is at least $150

SKUs with $0 revenue in either period are excluded entirely -- they are not
"movers" (they may be new, discontinued, or seasonal).

**Email output**: Show top 5 risers and top 5 fallers as separate sub-tables.

**Risers table** headers:

| Column Header | Description |
|---------------|-------------|
| SKU | Product SKU |
| Product Name | `short_description` from product_info |
| This Month Revenue | Revenue this month, formatted $X,XXX.XX |
| Same Month Last Year | Revenue same month last year, formatted $X,XXX.XX |
| Percent Change | Change vs same month last year |
| Year-to-Date Sales | Revenue from ytd_start through ytd_end per SKU, formatted $X,XXX.XX |
| Previous Year Year-to-Date Sales | Revenue from prev_ytd_start through prev_ytd_end per SKU, formatted $X,XXX.XX |

**Fallers table** headers (adds Inventory Value column):

| Column Header | Description |
|---------------|-------------|
| SKU | Product SKU |
| Product Name | `short_description` from product_info |
| This Month Revenue | Revenue this month, formatted $X,XXX.XX |
| Same Month Last Year | Revenue same month last year, formatted $X,XXX.XX |
| Percent Change | Change vs same month last year |
| Year-to-Date Sales | Revenue from ytd_start through ytd_end per SKU, formatted $X,XXX.XX |
| Previous Year Year-to-Date Sales | Revenue from prev_ytd_start through prev_ytd_end per SKU, formatted $X,XXX.XX |
| In Stock | `quantity_available` from bronze.product_info (integer count) |

**In Stock rules**:
- If `quantity_available` is 0 (out of stock), color the value coral (#FF7043) bold
- Query: JOIN fallers to `bronze.product_info` on SKU for `quantity_available`

**Biggest Movers YTD query guidance**: For each mover SKU, compute YTD revenue
using a LEFT JOIN from the movers result to a YTD subquery grouped by SKU. Use
`COALESCE(..., 0)` to handle SKUs with no sales in a YTD period. Query pattern:
`LEFT JOIN (SELECT sku, SUM(line_price) AS ytd_rev FROM deduped WHERE rn = 1
AND sale_date BETWEEN '{ytd_start}' AND '{ytd_end}' ... GROUP BY sku) ytd
ON movers.sku = ytd.sku`. Repeat for `prev_ytd_start`/`prev_ytd_end`.

### 3. Top States

Compare each state's share of total revenue for This Month vs the Same Month
Last Year.

**Threshold**: Include only if a state's share shifted by more than 5
percentage points.

Only consider US states (shipping_country = 'US' or 'United States').

Include states that had 0% share last year -- any state with >5pp shift
qualifies regardless of prior-year presence.

**Email output**: Show states with notable shifts using these exact headers:

| Column Header | Description |
|---------------|-------------|
| State | US state name |
| This Month Share | Revenue share % this month |
| Last Year Share | Revenue share % same month last year |
| Change | Difference in percentage points (display as plain number with "pp" suffix in the cell value, NOT in the header) |

**Important**: The "Change" column header must be the single word "Change" --
do not put "(pp)" or any other annotation in the header itself. The unit
"pp" belongs in each cell value (e.g., "+11.1 pp").

### 4. New Product Performance

Tracks products launched within the last 90 days. A product is "new" if its
`created_date_utc` in `bronze.product_info` falls within 90 days of
`report_month_end`.

**Filter**: `is_active = true` AND `created_date_utc >= lifecycle_start`
AND `quantity_available > 0`.
Exclude classifications `'Miscellaneous'` and `'Parts'` (non-sellable SKUs).

**Threshold**: Always include this section if at least one new product exists.
If no new products exist, omit the section entirely.

**Query approach**:

1. Identify all new SKUs from `bronze.product_info` matching the filter above.
2. For each new SKU, query `silver.fact_sales_items` (with dedup CTE) to get:
   - Total units and revenue since the product's `created_date_utc::date`
   - This month's units and revenue
   - Number of distinct months with at least one sale (sell-through months)
3. Compute derived metrics in the query or post-processing:
   - **Days since launch**: `report_month_end::date - created_date_utc::date`
   - **Units per month**: total units / (days since launch / 30.0), rounded to 1 decimal
   - **Sell-through rate**: sell-through months / total months since launch, as a percentage

**Email output**: Label the section "New Product Performance". Show a table:

| Column | Description |
|--------|-------------|
| Product | `short_description` from product_info |
| In Stock | `quantity_available` from product_info |
| Launched | The `created_date_utc` formatted as YYYY-MM-DD |
| Days Live | Days since launch |
| Units (Total) | Total units sold since launch |
| Revenue (Total) | Total revenue since launch, formatted $X,XXX.XX |
| This Month Units | Units sold this month (0 if none) |
| Units/Month | Average units per month since launch |
| Sell-Through | Percentage of months with at least one sale |

Sort by `Revenue (Total)` descending.

Color-code the **Sell-Through** column:
- >= 75% -> green `#04BA8D` (strong early traction)
- 25%-74% -> navy `#07043C` (moderate)
- < 25% -> coral `#FF7043` (weak early traction)

Color-code **This Month Units**:
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
  12 months before `report_month_end`. Stale inventory that hasn't sold in over
  a year is a catalog/SkuVault cleanup issue, not a monthly performance signal.

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
   **Also filter**: `last_sale >= report_month_end::date - INTERVAL '12 months'` to enforce
   the recency floor.
3. Left-join to a second subquery (grouped by `sku`) for the trailing 3-month
   period (`trailing_3mo_start` through `report_month_end`):
   `SELECT sku, SUM(quantity) AS units_3mo, SUM(line_price) AS rev_3mo,
   SUM(CASE WHEN sale_date BETWEEN report_month_start AND report_month_end THEN quantity ELSE 0 END) AS units_this_mo
   FROM deduped WHERE rn = 1 AND sale_date BETWEEN trailing_3mo_start AND report_month_end GROUP BY sku`.

Verify the row count matches the number of distinct SKUs before proceeding to
classification.

**Classification tiers** (applied after the query):

- **No Sales (3 mo)**: 0 units sold in the entire trailing 3-month window.
  These are the highest-priority flags.
- **No Sales (This Mo)**: > 0 units in trailing 3 months but 0 units this
  specific month. Only include if trailing 3-month average is also below 2
  units/month (to avoid flagging products with normal monthly variance).
- **Low Velocity**: Sold > 0 units this month but trailing 3-month average is
  below 1 unit/month.

**Threshold**: Include this section if at least one SKU qualifies for any tier.

**Email output**: Label the section "In Stock - Low Performance". Show a table
with products grouped by tier. Within each tier, sort by `quantity_available`
descending (largest idle inventory first).

| Column | Description |
|--------|-------------|
| SKU | The `sku` from product_info (multiple SKUs can share a product name) |
| Product | `short_description` from product_info |
| Status | Tier label: "No Sales (3 mo)", "No Sales (This Mo)", or "Low Velocity" |
| In Stock | `quantity_available` from product_info |
| Last Sale | Most recent sale_date, formatted YYYY-MM-DD |
| 3-Mo Units | Total units sold in trailing 3 months |
| 3-Mo Revenue | Total revenue in trailing 3 months, formatted $X,XXX.XX |

Cap the table at 8 rows maximum. If more than 8 SKUs qualify, show the top
8 by `quantity_available` descending and add a note below the table:
"Showing top 8 of {N} low-performing in-stock SKUs."

Color-code the **Status** column:
- "No Sales (3 mo)" -> coral `#FF7043` with bold
- "No Sales (This Mo)" -> coral `#FF7043`
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
  GOOD: "Total revenue $156,769.43 across 2,575 orders, down -5.9% YoY"
  BAD:  "Total revenue was $156,769.43 this month (-5.9%), on 2,575 orders, down from $160,341.14 and 2,590 orders last year."
- Do NOT restate last year's figures -- the tables already show both periods.
  GOOD: "Website revenue dropped -17.3% YoY to $85,975.08"
  BAD:  "Website revenue declined -17.3% year-over-year, from $101,407.55 to $85,975.08"
- One number per bullet. If a bullet has more than two dollar amounts or
  two unit counts, it's too verbose -- split or trim.
- Mover bullets: name + percentage is enough. Skip unit counts.
  GOOD: "Key Fob (ua-fob-bk) led risers at +172.7%"
  BAD:  "Key Fob (ua-fob-bk) led risers at +172.7% vs last year, with 39 units sold vs 14"
- Low performer bullet: just the count.
  GOOD: "11 in-stock SKUs flagged as low performers"
  BAD:  "3 active, in-stock SKUs recorded zero sales over the trailing 3-month window"
  Tier names like "No Sales (3 mo)" are internal labels for the table -- never
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
        Monthly Sales Brief &mdash; {report_month_name} {report_year}
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

Subject line: `DefenderShield Monthly Brief - {report_month_name} {report_year}`

### Table Header Rules

All table column headers MUST use full, unabbreviated words exactly as
specified in each section's column table above. Never abbreviate headers
(e.g., use "This Month Revenue" not "TM Rev", use "Orders Change" not
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
  "this_month_revenue": ...,
  "yoy_month_revenue": ...,
  "yoy_change_pct": ...,
  "ytd_revenue": ...,
  "prev_ytd_revenue": ...,
  "channel_breakdown": {
    "Website": {"this_month": ..., "last_year": ..., "ytd": ..., "prev_ytd": ...},
    "Amazon": {"this_month": ..., "last_year": ..., "ytd": ..., "prev_ytd": ...}
  },
  "this_month_orders": ...,
  "yoy_month_orders": ...,
  "movers_risers_count": ...,
  "movers_fallers_count": ...,
  "movers_detail": [
    {"sku": "...", "this_month_revenue": ..., "last_year_revenue": ..., "pct_change": ..., "ytd_revenue": ..., "prev_ytd_revenue": ..., "in_stock": ...}
  ],
  "low_performers_count": ...,
  "fact_check_passed": null
}
```

The `movers_detail` array MUST contain every SKU that appears in the Biggest
Movers section of the email (both risers and fallers). Each entry records the
exact values used to generate the email table row:
- `sku`: the product SKU string
- `this_month_revenue`: revenue this month (numeric, 2 decimal)
- `last_year_revenue`: revenue same month last year (numeric, 2 decimal)
- `pct_change`: percent change vs last year (numeric, 1 decimal)
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
    WHEN sale_date BETWEEN '{report_month_start}' AND '{report_month_end}' THEN 'this_month'
    WHEN sale_date BETWEEN '{yoy_month_start}' AND '{yoy_month_end}' THEN 'yoy_month'
  END AS period,
  ROUND(SUM(line_price)::numeric, 2) AS total_revenue,
  COUNT(DISTINCT order_id) AS orders
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN '{yoy_month_start}' AND '{report_month_end}'
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
    WHEN sale_date BETWEEN '{report_month_start}' AND '{report_month_end}' THEN 'this_month'
    WHEN sale_date BETWEEN '{yoy_month_start}' AND '{yoy_month_end}' THEN 'yoy_month'
  END AS period,
  CASE
    WHEN marketplace IN ('WooCommerce', 'Shopify') THEN 'Website'
    WHEN marketplace IN ('Amazon', 'AmazonUS', 'AmazonCA', 'AmazonAU') THEN 'Amazon'
  END AS channel,
  ROUND(SUM(line_price)::numeric, 2) AS revenue
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN '{yoy_month_start}' AND '{report_month_end}'
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
this_month AS (
  SELECT sku, ROUND(SUM(line_price)::numeric, 2) AS tm_revenue
  FROM deduped
  WHERE rn = 1
    AND sale_date BETWEEN '{report_month_start}' AND '{report_month_end}'
    AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
  GROUP BY sku
),
last_year AS (
  SELECT sku, ROUND(SUM(line_price)::numeric, 2) AS ly_revenue
  FROM deduped
  WHERE rn = 1
    AND sale_date BETWEEN '{yoy_month_start}' AND '{yoy_month_end}'
    AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
  GROUP BY sku
)
SELECT
  t.sku,
  t.tm_revenue AS this_month_revenue,
  l.ly_revenue AS last_year_revenue,
  ROUND(((t.tm_revenue - l.ly_revenue) / l.ly_revenue * 100)::numeric, 1) AS pct_change
FROM this_month t
JOIN last_year l ON t.sku = l.sku
WHERE t.tm_revenue > 0
  AND l.ly_revenue > 0
  AND ABS(t.tm_revenue - l.ly_revenue) >= 150
  AND ABS((t.tm_revenue - l.ly_revenue) / l.ly_revenue * 100) > 50
ORDER BY pct_change DESC;
```

### FC-4: YTD revenue validation

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
    WHEN sale_date BETWEEN '{ytd_start}' AND '{ytd_end}' THEN 'ytd'
    WHEN sale_date BETWEEN '{prev_ytd_start}' AND '{prev_ytd_end}' THEN 'prev_ytd'
  END AS period,
  ROUND(SUM(line_price)::numeric, 2) AS total_revenue
FROM deduped
WHERE rn = 1
  AND sale_date BETWEEN '{prev_ytd_start}' AND '{ytd_end}'
  AND marketplace NOT IN ('Manual', 'TransferSaleHoldsPendingQuantity')
GROUP BY 1 ORDER BY 1;
```

### Comparison Rules

For each metric, compute: `abs(metrics_value - fc_value) / fc_value`

If this ratio exceeds 0.005 (0.5%) for ANY metric, the fact-check FAILS.

Metrics to validate (all checks must pass):

| FC Query | FC Row | Metric in metrics.json |
|----------|--------|----------------------|
| FC-1 | this_month / total_revenue | `this_month_revenue` |
| FC-1 | this_month / orders | `this_month_orders` |
| FC-1 | yoy_month / total_revenue | `yoy_month_revenue` |
| FC-1 | yoy_month / orders | `yoy_month_orders` |
| FC-2 | this_month / Website | `channel_breakdown.Website.this_month` |
| FC-2 | this_month / Amazon | `channel_breakdown.Amazon.this_month` |
| FC-2 | yoy_month / Website | `channel_breakdown.Website.last_year` |
| FC-2 | yoy_month / Amazon | `channel_breakdown.Amazon.last_year` |
| FC-3 | per-SKU | `movers_detail` (see below) |
| FC-4 | ytd / total_revenue | `ytd_revenue` |
| FC-4 | prev_ytd / total_revenue | `prev_ytd_revenue` |

**FC-3 per-SKU validation**:

For each entry in `movers_detail`, find the matching SKU row in the FC-3
query results. The check FAILS if:
- A SKU in `movers_detail` does not appear in the FC-3 results (phantom SKU)
- A SKU in `movers_detail` has a `pct_change` that differs from the FC-3
  `pct_change` by more than 1.0 percentage point absolute
- A SKU in `movers_detail` has `this_month_revenue` that differs from FC-3

When reporting FC-3 discrepancies, list each failing SKU separately with
the metric name format `movers_detail.<sku>.this_month_revenue`.

After comparison, update metrics.json:
- If ALL checks pass: set `fact_check_passed` to `true`
- If ANY check fails: set `fact_check_passed` to `false` and add a
  `"discrepancies"` array listing each failed check:

```json
{
  "discrepancies": [
    {
      "metric": "this_month_revenue",
      "metrics_value": 150415.90,
      "fc_value": 156769.43,
      "pct_diff": 4.1
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
4. Do not make forward-looking predictions ("next month will likely...").
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
- [ ] Biggest Movers excludes SKUs with $0 revenue in either period
- [ ] YTD date ranges span Jan 1 through correct end dates
- [ ] All 3 tables (Sales Snapshot, Risers, Fallers) include YTD columns
- [ ] FC-4 YTD validation passed
