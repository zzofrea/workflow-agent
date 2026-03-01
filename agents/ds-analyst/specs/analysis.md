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

**Email output**: Label the section "Trending Products". Show a table with columns:

| Column | Description |
|--------|-------------|
| Product | The short_description from product_info (human-readable name) |
| Trend | A plain-English phrase: "Now gaining momentum", "Starting to slow down", "Picking back up", or "Leveling off" |
| This Week | Units sold this week |
| Avg/Week | The 90-day weekly average |
| Context | A one-line explanation like "Was steady, now selling 40% more per week" |

The Context column is critical -- it tells the reader WHY this product is
flagged. Use the relative slope to compute a human-readable percentage change
rate. Example contexts:
- "Was steady, now selling ~40% more per week over 90 days"
- "Was gaining, now leveling off at ~15 units/week"
- "Demand dropped ~30% per week over the last 90 days"

Color-code the Trend column:
- Gaining momentum -> green `#04BA8D`
- Slowing down -> coral `#FF7043`
- Leveling off / picking back up -> navy `#07043C`

Do NOT show slope values, p-values, R-squared, or any statistical terms.
Do NOT show raw numbers like "2.1 Steady -> Gaining".

### 4. Geographic Highlights

Compare each state/region's share of total revenue for This Week vs the
trailing 4-week average.

**Threshold**: Include only if a state's share shifted by > 5 percentage points.

Only consider US states (shipping_country = 'US' or 'United States').

Output: states with notable shifts, showing state, this_week_share_pct,
trailing_avg_share_pct, shift_pp.

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

Color-coding rules for data in tables and bullets:
- Positive changes (revenue up, units up, "Gaining") -> green `#04BA8D`
- Negative changes (revenue down, units down, "Slowing") -> coral `#FF7043`
- Neutral / informational -> dark navy `#07043C`
- Use `!` after especially good news in bullets (e.g. "Website revenue up 14.5%!")

### Personality and Tone

You are an eager, enthusiastic data analyst who genuinely loves finding
patterns in the data. Write like someone who is excited to share what they
found, but stays grounded in the numbers. Think "sharp junior analyst who
did their homework" -- not a robot, not a hype machine.

Rules:
- Start every email with a single short motivational line before the summary
  (not cheesy -- uplifting and work-appropriate, e.g. "New week, fresh numbers --
  let's see what happened." or "The data's in and there's some good stuff to unpack.")
- Use `!` on especially positive findings (big revenue jumps, standout SKUs)
- Keep it professional but warm -- you're talking to a small team
- Still NEVER make recommendations or reference inventory
- Still NEVER fabricate data

### Summary

Write the summary as a bulleted list (HTML `<ul>`), maximum 6 bullets.
Each bullet is one key takeaway referencing a specific number from your queries.
Do NOT write a prose paragraph.

Color-code each bullet:
- Positive news -> `<li style="color:#04BA8D">...</li>`
- Negative news -> `<li style="color:#FF7043">...</li>`
- Neutral info -> `<li style="color:#07043C">...</li>`

Rules for bullets:
- Reference ONLY numbers from your query results
- Do NOT make recommendations ("you should order more")
- Do NOT reference inventory levels
- Do NOT compare to industry benchmarks

### HTML Email Structure

Use the DefenderShield wordmark as the header logo and the Rusty Data logo
in the footer. Both are provided as base64 data URIs below.

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
      <img src="data:image/png;base64,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" alt="DefenderShield" style="height:28px;" />
    </div>

    <!-- Body -->
    <div style="padding:24px 32px;">

      <!-- Motivational opener -->
      <p style="color:#5D53C9;font-size:14px;font-style:italic;margin-top:0;">
        {motivational_line}
      </p>

      <h2 style="color:#07043C;font-size:20px;margin-bottom:4px;">Weekly Brief</h2>
      <p style="color:#61B6FC;font-size:13px;margin-top:0;">
        Week of {this_week_start} to {this_week_end}
      </p>

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
        SKU Movers
      </h3>
      <!-- table -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        Trending Products
      </h3>
      <!-- table -->

      <h3 style="color:#0553DF;font-size:16px;border-bottom:2px solid #0553DF;padding-bottom:4px;">
        Geographic Highlights
      </h3>
      <!-- table -->

    </div>

    <!-- Footer -->
    <div style="background:#f4f5f7;padding:16px 32px;text-align:center;border-top:1px solid #e0e0e0;">
      <img src="data:image/png;base64,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" alt="Rusty Data" style="height:32px;margin-bottom:8px;" />
      <p style="color:#999;font-size:11px;margin:0;">
        Automated analysis by Rusty Data | Powered by Claude
      </p>
    </div>

  </div>
</body>
</html>
```

**IMPORTANT**: The base64 logo strings above are already embedded. Copy them
exactly as-is into your HTML output. Do not modify or truncate them.
with the actual base64 strings stored in the environment variables
`DS_LOGO_BASE64` and `iVBORw0KGgoAAAANSUhEUgAAADwAAAA8CAYAAAA6/NlyAAAjRklEQVR42uWbd3RVVfr3P/uU29J7IbTQpEkJIogUsYAKgkAQdSzgGNuggm10GEPsZZwBuzh2RYeICoIISAmg1NCTEEpI7/0mt55z9vtHAoN1/K01v3etd70nKys599x99v7up+xnP893w/9nl/i/3V9mZubZPhcvXiyFEADy//2JzMxUIF0FlN/ZRgFUQO1o+78jDPFbksjKyrIApJRCCPGbUpBSitmzZyvZRUUKublBAE0RKKqGPxCwfZ37tdZUajiLiosjPI1tuh+s4b3jPPHx3d1XXtk/AD2CuqaahmmdeaVKWpqSOWWKeWYc/3XAmZmZSqeaSSmlAIQQwur8n18CfRZodq2AHEMFdu/bGHHvkx/3bPd6h7V6fcOamloGtvr8KRIZAzJESktDgkB4FU1vcepadajTfjw8OuxI98SE/d3jok98/OoLp33+IABpaWn6vn37jJ/2f64gMjMzlaysLPmfzONngM/YlqIo0rIsBTh3AjgDXAiBlFJJy8hQc5ctC9p1jSlzF4zI2Z97hd8fvMxnMEoVwpkYE0HXpEQSYyOJjY4gPDwEm6ZhSXC3e6lvaqGuvpGK6gbK6upxt7jRVbU4IiZiW2pC9Kbb0q/aeMdNN1UBZGRk6MuWLTM6sHaAPTOu7OxsJS8vT3Zqg5BScub5uRP1I8CdIM59Js9pzJm/mZmZIj8/X2RnZ5uqgOlz7xmx59jxha1eY2JEZFRCcnQYIwb0lTMmjTX69+kjYmNihKbq4pdNyJKYQdnU0ipLK8vYdbBQbNlzWN209xCNNQ2EhLnyuyXFZN8+a+wbC26/v0aCSE9PV1asWGH9ktb9FOBvAv79ZjBehRwjM/OFxH/t+GFxVVP7DeEhjrCEyBBMwwy2BQyhgBrucgi700lCdCSJyQl4XU7aHDbMEAe28FDCw8KICw+li8tF//BQBsfHEO0MlSCshrpqa8X6Hco72WvVvPwTSN1WOqhfyotvLbr+3REjrvGQlqaTm2v8Tz38/wjwGTvRVEWOSb9l1p4Dx57xWkofu67i1BQjMsypjB4+UPlu2z7uunkmF6UN4UD+MU6XVlJWXUttXQOtTS00enw0WRLLpoHLDgP6Yu/bi3ibzpC4GCb3SOGK1K70iU2SWD7r46+/45V3V6h7DhaSkBy36erLRj/03lOPH5Dp6SrZK6wOJfzPjvVngH+tkQBmpaer2dnZ5vHj39ivnf/+s3l5JxdcNi6NiIgwc/ueg+qTC/7I5Alj6ZqcwPBJc8haeAfXTL7iXNUlGGyntcVNVW0jVVV11Dc0sWNnLh99vxft5hn4LROP3wBp0SXExaVdE5g7uD8TevbF62mWL3+QLZ9flq34fN76EYP6LNr5xQdvGZYUZGYKfqcnV8+9ycrK+uVvpaer+dnZ5oqNKyJuWPjPD4tOlc27NX2y8cmSp+WhI0fV1F49SIqL5JuNOzivbw+WLf+Sqy4bQ6/uKezev583PvqMPj27YNNt6DadxIQ4EpLjGDF0OHHxUXzw3VaUgf2wCYUImw2XzUZr0GRfXQOrThSTX1XF0KQEMW38BDFp3DBj36GCkN2HCqcMvGics+FU/nfWli2ycx2Xv2ex/80rPT1dJTvbXPHO0rj7Fr29sramYcazj94ZfO+lx1Wfaqj7a+pZuz6HLzdsZ932nSxf9S1hEaG4HHZU1cbqrT/w7F//TlFxKXa7jpRw8OgxhkyYxaadu6mtrscnJULVsExJSzBASyCAQ1OJtzmQispHp0qZ8K/VvLV7J0MHnKdt/uxl67qp463DR44/0mvijNf37dun/14TVX6q0j82WpTs7Gzr0Ucfjbv3zTUr6xvcl772xL3BR+68TS+sqhRTv1jLpuIKKkoqWbr4AWZPn8y2vYfQNBthYSEAOGx27HHRhDideDw+HHYbJaWVFO85QG2zG6/fjyEEmqbiCQbpHR7CRYmxuH1+DEWgawpxDhstRpA7t+8mY+16AhL1kyVPKPfeOt0sPFF6140PP7Pk6IoVGoxXzyytv4bpRw8XL14spJRCSikyMzMVshAvPvCA65Ocg29VN7SMffnJ+4N/nDNH31dykmtWb2J3TRMhZhDF6UDVbRSePE1ifAxCQGRYCEHDw9wZk9j09QdcMHwYEWGhaJqLwf17c9WsKVw4uA81dfXgcGAKcKrw7EVpLL9iHP0iwwiqAvfqDXgO5OFKiCW6uJy33/iEOd9spqKpWSxd/Ih6z01Tg4UnS+6et3z1o5qyzdi6dasCUvwuCWdlZVmdQYXMysoXisB893Dxw6Xlddc+cf8tgTvnXKfvKTnJ7G+2UOxtJ1ITaEKhR3I8l8zKoL65mTHDB+H2eAkLDcHtdtMlKR53aytPL3mLT1dvYEPONtq8Xt596wVSU7pTXVcHDjuKouAzLT4qOMnf9x2myjIJFpxgkNdPeKsbo6Qcz+ZdRNY18+3pMv6wbhM1TY0szVyoXTdtYjD3cOGicdffem1OTo7B+AnquUHSf7Th9PR0FbLNyTdkXH3sZPHj6VPGmX+9Z56tqLaC2zZso7TdQ4zTic8fxGjz8tTCDOZeO5n3nnsch82Bw2YjIS4Bu92GqtooLCpm0cNPc8PNDzDp6rmMm30XdbX1mJZBXVML2G1ICXZVZ3VFDa+dLiNoScShfF7KeojeTgdtn34NhonfppIY4mRreS23r99K0DTEK1kPqYP69NR27yt87f5n/55KTo75a9h+pu+ZmZlKdnY2n3zyVuz3B/OW9ExJ4sVFC0R7wOKOjTnkNbcSa3cQCJoopoUiLc7r14e/PngPDW4PW3btJWAY/LDvCFW1DdQ11HPf3OvIXvUOczOu5/b5t7DqnSUM6tcbVVFob/eA3Y6wJKqQOKprcBw+hm/VRnpoGoOHnE+sMwSjrBVT1bH37o5PUYh32vm6uJyHvttGXFSM8tpTC0yhqUkr13z3kk1TZWZm5i+qtMaPjVisWVOlqoLgU++tf8ztNXovXTDP6p6UomRu3sR3FVXEhrrwB4MomgbCxGzzMP+x52hvb6O8upZGXwCpaUzPeIDIkFBCQpzExccyqHc3evVIoVtyPD6Phx8O5NE1KYnq+kZEqAtsGqpH4t26G7O6gUAgiK9rEgWlFTTW1XD3GCfrSt2U1jURqwn8fpOYEBdv5B1nTJcE5owYpd71h+nm399cPu3S9FtmZmVlrRw/PlPLyckyfjXwGD9+vJaTk2POvO22wau3568ff9GwhI3vLZE7Tp9Wrlm9EUOAJkBKsBQQEsxjp2g7XY4wJbZAENUykf4A3qp6kBLV5cBsbYV2HwgFNBUl3EW4zYbTaaPZG0S7/mq08FD8a7chq2vwSo0BIR5GdQ1lR72L+3rWcrTB5IRzAIUlNdR1TSTkyvHIdi8ey6J7iIuNs6YRJjBHXHu72tjq3nvsw5cnJgzK9mRmdvimnwUeGRkZutq9u563c6fx2N/fXtDk9k5+95kHjS7Jidr9m7dxqKGJCJsds2Nf1zFbUqAmJxE2qB/O/n3Qz0vFNqgP1DcxIbUbIREhTJ0witeefphxY0cw9uILsEeEc7qiCnVgb7x9eqJdOBQV8K3cgFlVj+qwoQiFkX1SmJPUQFpEO5uqbPSbeT/LnnmcS8YM4/M3P8SrCLSe3bEHDco8XuyWyZSB5wtLBqxV63JSNhccO1597JWDfftNcGZk3MCaNWusH9lwYWGSHBAe7l/01EspZRV1cyaOGS7HXZimbCkqYkNpFRE2J6ZlnY1lzm59vD6CrW0Y7lZUVaWtoYW4lja+/WQZy/+RRcHxU/TqnsrN6Vez8I55PHTnH7D5AwiHE9clY1AsA+8XGzBaPOhR4XhqGlh813Wkz7ubtXkNmJ52GDaDu669hInTricuNpaPXn0G//rtyPJKFJcLFwqfHD/N6YYqMe/ayTKlWwrFZTWz9u5b7YKqYGVlpfyZhEtK4sX2ba9bHlfUtLKa5rlPL5xnDurXR3ssZyeHGhqJsNsIWrJDLQVIBFJIhAKKoiJVBaEKtAN56NV1dEmKZ+P2PdTU1TP3umn4AwEEAXYdzCN7zWb0yFCU/r2hoYlgURmaruBtcXPBkH688sSjbP9hJ9mHG+gTGsRsruaw28Xw1GSSkuMZnXYBIS4nq97LJmL4AGxOF5XNLcQ67Fzer78oq6iQW3bs7dYeVFe//9orFTk5E4Ac+RMvnW2ZltRKqxqnp3ZNkBNHDeVEfR0by8oJd9jxSxNLFQhVIBQNoahImw6tbgKrNmBrbKZh3VaudDp486W/MGfO7by+fCXPL7qfw/nH2LRtF7oeSmtLK4Y/SKC0muDmnfh3H0IJGlhBA6dNZ+kTD6LrNnJ27OTVJ/5MafIEXDWHKPz2fVIGD2FQvwGcqqigtracUa42Kt/PxvR6cYSFsPJ4ET7DJ2ZcfrGlh7ic+/NLJ3ZEWlnip15aSCmVNz76KLq+sfGSy8ZNEvFxiepHO3fT1O4nLsSODw2lupZA7kGEw470G2gD+yF7JKONvQAZH41dwv4D+dww08sLLyxm75E87s/8G02tbtpb3BRV1OL3eFFHDCDU5cS9dSfS7kC16/gbW3l20XxGDx9GS2srDW4fphHgqUWPMffeelKrt/PBojs5kj6f6sJcogvXkJqQSqitG3tXfYtz8njyg0F+KC7lwoH9xMCeXeXp4vLJwFLgrKdWOoIM5ITrbxu2YevuP1hSRo0aPkCCKjYXl6GEOCEsDNVlR02OQ7voArQR56NfOBQlJREl1IWIj8UwBWETxlDUtyfXP/MqeSeKGHheb/JPlrDs2UfZtyGbZR+t4NOVX2OGRtMcm4RfD0GxOfC3+bhhxpUsvO0G6hrqCAt1MbR/b55e8gYxYQ5WvPsWcVffS6+EMJYuXcrePbup7zKWtPQ/8eB9c0kfdB6BnD0EFYWNJeWEhEYrg/ulCrffN+K199+POZMSAlDznU6NqiqrXokY29rufU4ahmPhbdcr4dFhPLXnAP7TZVj785DFFRjlVVjudqzmVmRrG1ZxOfJ4ESIyDKFrGJZB2Kgh+J12itZtIrVbF6rqmmhobOJQYTG7tm9mekoLE22txFWeJtylUNEcwDQMXslaQK/uXdF1HU1zcFHaEFat38LbK9bQ2ubBERXJyxtO8pfhJiFWG8bQ2Tx5xwymzZjH5ZMvpaC4nKa4KJwOJzcN6i/KKsvlNzv2OzQp1hce3FOUn5+v5ufny7OBh1AUq6SmMaJ3cozo26M7hyprqPN40DUd0+kElxNFU8A0EYaBsGkQFYE0TITLiRbmwiYlgcJi2nNPEB8Ty0t/fZAnX36Tl99Yjqoo3J4xj93f76ayoIAJqaHE0IrsmsB5I8fx2POvoQiF5MRYNF1DCJW2gMnOvYepOZ7L5BSTW5NC+SjXS2L37pgFubz0Dqz+8hO++XotFS2thEVFcLKhgdaAhwF9Ui2XTVer6uv7C/iutrZW/CjSkpalCIllCUWNiwpnR3sbHimJOa8nYmAvREMz1ulyzIoazPZ2hKajdEvGPnwAAa+P9qOFyPYAkfv2c3dsM7uD4YyZcy92q52ifetZuW4TK1d+TXmzyaAojfZ2gz3e/rz94kLGX5hGeWU5+44e52RxGS3uVqIjY4iJieNIXgFDE3WapSDHF0PVoGRaamt49a5reeXjL/jsX+so1zXsF49EUVQaPe2UtrhJjosnzOmguaUtVQI5OZ1OKw3IBfokRfpO1bZbp0uqxDW33idjxowQ9vpGjPJqgiUVWEXlmC1tHVlNXYWARD1Rivv7vYSEh5EWF41LtVFgmCwYZUdTLV7avp0N7i588u33lBQcY/OuI3SJU1kyw8Zt3/l54cl7GX/hSCqqS4mKCGf65eNAaIAF6Dyz9HXahM5X/ScS1HRCusYSHRlG7a793PW3t4iLiaQgJZ6QC9PQVQ3LH8BvSWo8PgZHhONw2nH7jPiO9Gu8BNCmTJli5ubmcvfMq3L/tOTDpqmXjUkSRoCPH30ONT6OdkVFsSykqkBYCIoiwOfHlhSKu76FaSOHkTn/VlK6dsXpDOGxV/7JrV+8w/qZIbwyK5ZvCtq56aEFDBk7gYM71/LS4wvYcDyfk+5ousaG4fM1Y9dt+AMB2r1eLMtCCIXQEAff7zuCAGIHdMPUFExvAH9jM660QTR170Jt0CAiJRlfmwc14EfVBEZA0ur344yMQLNptLjbXb+4W7r88svb7LpmFhw7xSPz5/HPZS8wcdQwwkJDkDYHqs2OoqiYza04e3fDGxfFtLHDWfnO3xg8ZACKBqEhdob36c7BGsGVawVLdwQYFCd4YlI0FXXNrMj+grV59SzKFYREhJMYG4lpSYQARVGw6TqqquF0OWjz+KiqbUB6vASq6wi0+5H+IIpQMNs8OCLCCY+JobGhgUhVEMQiaEkUITBNC1VVOmIFK/jLGY8ffvhBuGw2TpRVccmsO1n97TZavX6CltURSQkFs7EZR2o3zP6pJDa38UrWnzGCQZqaWpCWREqL3GNl3Hy+4PZBGh8eg2nfCF7dFeDqi4ezdc8hLtSr+OtIOzEJscRGRRIIBFHVfyf57XYdm6bR2NxKU1sbTk3DbGhGRIRi2W1YNjtodix/EI/Pw7D4WFZOm8SNfVPxGxYKoKkKlmGBaaFr+o/3w4sXL5aAuOGGG9pb3G3e0aOGc/WEUfKrT1ex8/sD+Fo9SF8bVls7eq+uhM64Avfh49x77SS6du1Ca1sbdl0D2VG5sAQE/QY3DZVsmSF4Y5yCVwouuehCmk3JPWl2FNNPRHwKdpsDwzAAgaIIDMOgvqEJh91OU3MLERGhXHD+eQTyTqIUlSGra5D1teB2o9htoKpIy+qI6UWH5SuKINxup83rI+APoGpqewfUTi/dmQJRnHZbQOk7usHweft88s4LLL18HFlfrEU3LCybjpIch61/b9rqG4lvdXPN1En4/O1oqkDSUcOyzCBXXTSE2z928I+trdw8yInpbSOxZy/A4tDug6we6WRzfh33PzkCyzKRUiKlRFFUFEWy6IU3OHriFKZUKSopR9d1PC1t+Esr0e06CBWJxDakH47RIzjS0Mys1d9S7zewKQoOJMmhThrqmvD7fEREhFTW/jwBkC78gWwSHHpZTXXDqJZWj5w6eQLPNtSjKBq6KrACQVRFwVdSwcU9UuianITf7+dMNVvTVJpa3Vw94SKefTqT55f8kw8r2qgsbeKm20fR5vMTafPyZZGTmXNu56Yrx9Le3o7L6SQQDGJZEpvNxouL7mPPoXxqGluwTImu6ZRUVfPGJytxt/lQVYmU4P/hAGpsLK7zelHn8eBQVXyWQbTdQfeIcHKOHKfF4yc5Pu5Ux16/Y2n6UcYjPjr6aHFlbfqx08UMHzqAeAuqvB6cqoJit6Hb7BiHj3HRjKtwuVw0NHpQVRVkxyBCXaG8+sGnzJg0keuuXM7ry9fw9JI3WDDvBrbuyqXN3crgwQN48oEMhAruVjdCCCIjwmn3eAkaQcJCQ7h28uSfpWZsqkLmc69ji49BmhaWUAgeKkDv3Q1N01AkeIMWfZMicGoujh4/qfiCQXp27ZJ/AIiPj//3bik9vUNKSQnxu1vbPew9UqCEOcIZlBCL1zJR7Xasunoq3llOL6Fw/YzJeDztWJbEtCyChklUZAxrN+/gwcyXeP/zbzAMk9lXXkx8dBiPvfA6KXGRPPLQw2zflsfGLTtw2MNobG0j45FnqKiuJzY6kbiYWFwuB3mF+bz+zgdc98c/MW7mzZRUltAzMRZSu+G8aTrOyeMQIQ4CJeWI46fBZuswKWlyYZdkACv3aIFQpFk/bfKYvM5yqnVWpc/c3Hb9+P2bdu6r35mbF8vtQXlFj65iVVkVZk0tvi/WMW/caBbcdxuJ8XF4/T6cTjt2m46qOVi9cSu3P/oshu7kqZff573PV3P91ZfxwO03sX3fQRa9+gHutnZ6Jtv5+POvuGrSRAb2HUBUTAyTb57PPTfORJgm3+3Yy64jBTQHDcxuXeB0KcdPldLa3AIRISiJcYioMOwVlXh3HsK35xCu7l0IOuyEaiqTU7vS0FArD+afklHhkbtvvvballv+Xfo9q9ISEOlXpjfeG/n2xp0H8+eUlJVZU/t0V585eoyybzfz4NTLePHpxZimB1W14XIGaWxs4mDBCT79cj3/zF5HEHC6QjAtk5PFVTy5bDmRYS6mjBxGnDOUwj07eWNKJMuP7OOWe7PoHh3CoQOHOXWqjIXPvAKJCZAch2viRUT06oYzKY76tz8jzKHT3NYOioLiD4A/gD50IMHCEoLVdcgDebSNHMLo6GiGJyWzbtNWWVhSJc4f1Hu1pihGJ07jJ1nL8aqiCGPMzFu/3JGbd/36bXtkxo3pTIuP49XaBqZMmgDAsZOnWf7ZKk5XN3CqsopDhcX4W93YoyOxK4Kgpx1hmYQPOQ/b+JF4G5r5OP8ENLYQ1q0Pf9zdiq6GULf9exASNSGWiCmXovVIQcRFITQVw7AItrbjPVCAVdNAt5QUPL4c0DSkpiACKsKyUG0qlqJh1jRgBQNc1z8VRdisz7/doSh+f3P/HrHfH5ISMjMtOguF5wDeakopmHHR+Tv2Hz127L0v1p2XceMcY2CLW+sdHcmoUaM4dPQI0+94kGKHE85LRbckGBahyQkEWluRmoqeEo9+fn/o2wO/oqH3iSJ2YF8wggTb/RhBE0NAnF1DahqWqmJJiRk0kW0eFJuKeaKEsB920uRu54FbbyQpKZmDhwtRIkJBgn/dVoJFJSB0dE3F2yOZAfGxpJ/Xj8rKEnPV1p16XNfEbz/9xz+Of7ZkiXpuKfUcwEKSlqY/9MDCqt6XTv/4QP6JrHVbN8sPV64lY/Y1aKrGLQsepzgliaQZV2HV1eHZn49h0wm2e7GPGobSqzvERIJmg6CB2taKebwOQ1GRmoKqKOhCIAX4OtkOWBLRGWUpcdEIuw3jQB6fjW7j82KLgecP4dkXl7ChoJC4e26m9mgR6e2FXHuJkxtXteEc0BP/gF7M69WN+Ig4+edlS5WW+kZj1KiBnwkhgmlpaXpubq75i4n4tLQ0cnNz6R0f8W1ZnfuBqXdnRg3plWw9cOc85Ynnl3LI5yfxmsvx1dUTXL2FQH0rYGG/OA11/EgsXxAMAwJBsOlIjw/reBFoKhYgLTCQHYXcDvYIUiggJUIo6LEj8Hu9xPiaGRyvkVNjMe/uR7CSogi7cQbS4QC3B5dNYPqD2JLi8I67gAtiY5l7/lBKy4us91esVWPiYjd//9y89cLyqbnZ2cavVR5E7rJl0qYI1uc3Tri5RyC8sr7ZWJj5irpnzwFeePczwudMw2hsxli/g2BJBbquovXujn7BYMzmFjQJQlE7spp+PzIuGn3aJGTnj0B0ukt5rl51+FCrI+Jy+H0E7E6mrDco1mNwXdIDx7CBGLqG5fUS0yuRdQWxrKmMQJ85GsWm89zI4USGhHPf48+JuvpG88qxw58VPS/xZWRk6G9JaXWy/X5WeRCAvPqGu6J+yD+xatM17ot2VavW5tDJ+slD++SD8zPEvlMnWbL0PcIiI3EbFgRN1PAQpFDBMkFRzvH5HWKUQvk3M0iIjkqmOLdL61waESgqtLRBwIKYMPQQO4bPD6aBUDSkEMigQViUC3dTI0sevJP75lzHV+vXGen3Pal1TYh9u3THmowZM2ep2dnZ5m+weDIVyLIiRqb3jKJ+5/ReZsLBRt2zdV+NOmbsMNuOL9+hob5BzLrjYbbuOsLwvpHc1NOLwzKQZ2t1HVKUUnYMzAIViab+ex6kJTuAncuwFGek3vG50BRUReD1WwQDFigd28egCaoiiQhzkLmxhiunz2HZS09QVFZsXDr7Lq3ZEyh8adHdY2+bPbvhTHe/RWoRgFw6f779nRO18wrq2gcnRkV82yXK1mvX7sMv/fH6q823n8tSi8tKxZW3PMAkRwFLpkbgbgqgKAoK8qz0pPh3V5aUNBsdrz+D05JnUEqkkJxbpJedE6YC0TbQOsvbhiFx6gqaS+eer2tp6DOVd//2BAF/wJwy916558jx9qmXpF3zxbLXt9FJ0/jdtCWFjiyO3wSZman02bL/5ZNlNfcsnv+HQOaC+XpJZZn4w91/Js27l6cvjcHts2j3SzTRITwhJKaEMF1wqFlh8V4wLatDo5FI2ck/ER2VDCnPmjESiSkVwjR4epRFjxBo80N8iEJrUPLHb1pwXDCTT15chCoseeP8R+RXG79n9PBBd+366uNlZ9hGZ/Clp6cr59z/ImAB6QpkKwMGpIv8fEwpM9Ve4x/9sLi06rq/zL8p+MTCe7XGlnpxy0PPII+u42/jXfSKtdPssfD4DRRFRaigCAiYUO9XsKSF7JTqWYrfWXOWSEVBFxJFduqihEibRYRDJVxX2Frm4eEfJONn3sY/Hv0TXq/buum+v7Jyww/K0IGpjx5Ym/38hAnj1ZyOYrj8qeb+GmDxU73vJKNZUkq957ip7xaX1/7htlmXGa898WfF7ghTsl57j6+Wv891ibXMPT+EmDAn87f52VtjEaIpHUVuvVNZO98sz7hm2eG1LdFh238eLhgWI2kLCGIcKnabSkGdhzcOBtgW7MnD993LjVOvoLSi2Lx14WKxbc9h2b9390V53331nOyok5n/FWJaZ2HZlFJqfS+59vkTRaULJlw4hJezHjAH9x+o7i84ztOvvkvDkRymxjVyQfdQosMcBAwTX8AkYAosIToCDMRZD36W2ykEmpD0DJXEhWoYUiGvysO/Cjxsa08kbeJUFt87l4ToGNZ+t8G4P3OJVlJd70kbNuCB3Ss/eFNmZir8m0l7llj6SwIUv8Z9/hnwDlYPqvKENeyamzMOHS38W1yYM+zuubPMv9xzKygudeOuXN79ZCV1hbvor1QwoavChSkuEp0Kmn5myRJn3DIonWOU4AtKChtNckra2VyhUmHrzsixl5Bx40yG9EmVNTXl1l//sUz95KvvsOl64ehBfeav++ydjZ0Oyvo1UtpvkktlhzdBCCHPJYhnZmYqiztoTWQJoShg3PHAn9M+z9n7fGNj66WD+/Vk4bxZxk2zrgJC1IKycrFi7Ra27dxDQ0kBkWYjXewBEuwmUTaJU1OQQFtQ0uSHSq9GedBOMCSRbr0GMvmS0cyYNI64iGjpbqkzX/lopfLm8lVKRXVdoGtK3GeL5133yNy5c6vT0tL0KecQyFesWKGmp6dbQgiEENKyrJ8J7keAt2zZotXV1SmzZ88OnGkMsHXrVntYWJg5YsSIYHp6uho1YIB9WVaWR0opLk6/9b5D+af+2ObxDBw6oC83XzPRnD75Etmze28FUBpbm9l99CQFRWVU1dXR0tKCzx9AUQQOu4OI8HBSkuIZ2rcnIwf2wm5zYZlB63DeEevTNZuV5d9sUirLqwiNic6ZMKT/q+s+evPzoGGRlpah5+YuM6SULF68WCxevFh+/fXXztTUVGPQoEEBKaWSnZ0tAGbPnm3+z1X6l5m1ANbbb7+d8PpXOemnTpde3+puvSg+MZ4RA/owenh/OWbEcOu81B4kJcb9jENzlnYa9FFSWcOhwuNix579yr6Dx9hzrAhvS6sVnRCztmvXxI8Pfvnxl0KIYGcR3/otXuW54/+PfOlzqXv/6cxDZmamtmbNGpGbmxsUwPsfvBLz/ob8tPKyyuklFVVXBKToFerQiAoPJzoukpjIKKIiwnE6bZimxOP10djcTF1jEy0NzTS42wkEg0aYXT/apUvymn59uq/76vX5+4Xo6QOUjLfeUi+LirJmz55tZWZmisWLF8vs7GwlPT1ddh5VUDozOKKpqUk5syEaMWJE8D+sw/yih/u1mezgduVr+fnZAQCbrrEi+/Owb3YdGnTo6LHRh0+VJXubGy0sBCZgdcbPigKqAhrEJ8RZA3t2Ozlu1LCdU2+4uGh0t5GeYOeBj7S0ND133z5D/oS5fy6D/6fHFM5gy87OFueq9H/9lEx6h8opP51BRXT+nkOIUc75/BdmXk3vOAb0Xz3O8796UCs9PV3Nzq4VjP8dX84B0uNl5oAB8r95bOf/++v/ABqDtgrr5GM7AAAAAElFTkSuQmCC`.

For a quiet week (no findings clear any threshold), use the same template
structure but replace the body content with:
```html
<p style="color:#5D53C9;font-size:14px;font-style:italic;margin-top:0;">
  {motivational_line}
</p>
<h2 style="color:#07043C;font-size:20px;">Weekly Brief - Quiet Week</h2>
<p style="color:#07043C;">
  Total revenue: ${total_revenue}. Orders: {order_count}.
  Nothing jumped out this week -- steady as she goes.
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
