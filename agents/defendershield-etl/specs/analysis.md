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
  current_date - (extract(isodow from current_date))::int - 13 AS prior_week_start;
```

Verify the output: this_week_end MUST be a Sunday, this_week_start MUST be a
Monday, and the date range must be exactly 7 days. If any of these are wrong,
STOP and report the error.

---

## Finding Categories and Thresholds

### 1. Sales Snapshot

Compare total revenue (SUM of line_price) and order count (COUNT DISTINCT
order_id) by channel for This Week vs Prior Week.

**Threshold**: Include this section only if any channel's week-over-week
revenue change exceeds 10% in either direction.

Output: a table showing channel, this week revenue, prior week revenue,
% change, this week orders, prior week orders.

### 2. Biggest Movers

For each SKU, compare This Week's unit sales to the trailing 4-week average
(the 4 complete weeks ending with Prior Week).

**Thresholds**: Include a SKU only if:
- Unit sales changed more than 50% from the 4-week average, AND
- The absolute change is at least 5 units

**Minimum history**: Exclude SKUs with fewer than 4 weeks of sales history.

Output: top 5 risers and top 5 fallers, showing SKU, product name, this week
units, 4-week avg units, percent change.

### 3. Top States

Compare each state's share of total revenue for This Week vs the
trailing 4-week average.

**Threshold**: Include only if a state's share shifted by more than 5
percentage points.

Only consider US states (shipping_country = 'US' or 'United States').

Output: states with notable shifts, showing state, this week share %,
4-week avg share %, change in percentage points.

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

Write the summary as a bulleted list (HTML `<ul>`), maximum 6 bullets.
Each bullet is one key takeaway referencing a specific number from your queries.
Do NOT write a prose paragraph. All bullet text is black (#07043C). Only
inline percentage values get green/coral color.

No motivational opener. No exclamation marks. Straightforward.

Rules for bullets:
- Reference ONLY numbers from your query results
- Do NOT make recommendations ("you should order more")
- Do NOT reference inventory levels except for the In Stock - Low Performance section
- Do NOT compare to industry benchmarks

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
  No noteworthy changes from the prior week.
</p>
```

Subject line: `DefenderShield Weekly Brief - {this_week_end}` or
`DefenderShield Weekly Brief - Quiet Week`

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
