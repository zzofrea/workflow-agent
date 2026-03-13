# DefenderShield ETL Behavioral Specification

## Scenario 1: Database connectivity
GIVEN the ETL service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries for all documented tables.

## Scenario 2: Silver layer record volume
GIVEN the ETL ingests sales data from multiple marketplaces.
WHEN the silver.fact_sales_items table row count is computed.
THEN there are at least 100,000 total line items.

## Scenario 3: Sales data freshness
GIVEN the ETL runs daily and ingests recent orders.
WHEN the silver.fact_sales_items table is analyzed for recency.
THEN there are rows with _created_at within the past 7 days.

## Scenario 4: Marketplace diversity
GIVEN DefenderShield sells across multiple channels.
WHEN the distinct marketplace values in silver.fact_sales_items are counted.
THEN there are at least 3 distinct marketplaces.

## Scenario 5: Gold snapshot volume
GIVEN the ETL produces a completed sales snapshot.
WHEN the gold.completed_sales_items_snapshot table row count is computed.
THEN it contains at least 100,000 records.

## Scenario 6: Forecast freshness
GIVEN inventory forecasts are regenerated on each ETL run.
WHEN the gold.forecast_depletion table is analyzed for recency.
THEN at least some rows have a forecast_date within the past 7 days.

## Scenario 7: Forecast classification completeness
GIVEN SKUs are classified by depletion risk.
WHEN the gold.forecast_depletion table is analyzed for null classifications.
THEN all rows have a non-null classification value.

## Scenario 8: Monthly aggregation coverage
GIVEN the ETL produces monthly sales rollups.
WHEN the distinct months in silver.monthly_sales_by_sku are counted.
THEN there are at least 12 distinct months.

## Scenario 9: Required fields integrity
GIVEN sales items have key identifying fields.
WHEN the silver.fact_sales_items table is analyzed for null required fields.
THEN at least 95% of rows have non-null order_id, sku, and quantity fields.

## Scenario 10: Price data integrity
GIVEN sales items should have pricing data when quantities are present.
WHEN rows with quantity > 0 are analyzed for null unit_price.
THEN less than 1% of such rows have a null unit_price.

## Scenario 11: Bronze watermark advancement
GIVEN the ETL uses incremental loading based on _modified_date.
WHEN the maximum _modified_date in bronze.skuvault_orders is checked.
THEN it falls within the past 3 days.

## Scenario 12: Per-marketplace freshness
GIVEN DefenderShield sells across Amazon and Shopify (WooCommerce is deprecated).
WHEN the most recent _created_at is checked for each active marketplace in silver.fact_sales_items.
THEN every active marketplace (Amazon, Shopify) has at least one row with _created_at within the past 1 day.

---

# Database Schema Reference

Connection details are in your environment variables (PGHOST, PGPORT, PGUSER, PGDATABASE).
Trust authentication -- no password required.

```
psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE
```

The ETL uses a medallion architecture: bronze (raw ingestion), silver (cleaned/deduped), and gold (aggregated/analytics-ready).

## Tables

### bronze.skuvault_orders
Raw orders ingested from the SkuVault API via incremental loading.

| Column | Type | Notes |
|--------|------|-------|
| _modified_date | date | Watermark column for incremental loads |
| _created_at | timestamptz | ETL ingestion timestamp |
| marketplace | text | Sales channel (e.g. "Amazon", "Shopify") |

~55k+ rows. The ETL advances `_modified_date` on each run; a frozen watermark indicates a pipeline stall.

### silver.fact_sales_items
Individual line items from all sales channels.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| order_id | text | Order identifier |
| sku | text | Product SKU |
| quantity | integer | Units sold |
| unit_price | numeric(10,2) | Price per unit (USD) |
| line_price | numeric(10,2) | Total line amount (USD) |
| marketplace | text | Sales channel (e.g. "Amazon", "Shopify") |
| status | text | Order status |
| sale_date | date | Date of sale |
| sale_timestamp | timestamptz | Exact sale time |
| item_source | text | Data source identifier |
| part_number | text | Product part number |
| shipping_country | text | Destination country |
| shipping_region | text | Destination region/state |
| shipping_city | text | Destination city |
| _modified_date | date | Source modification date |
| _created_at | timestamptz | ETL ingestion time |
| raw_unitprice_a | numeric(10,2) | Original unit price before FX |
| raw_lineprice | numeric(10,2) | Original line price before FX |

Unique constraint: (order_id, sku, item_source, _modified_date)

### silver.monthly_sales_by_sku
Pre-aggregated monthly sales rollup per SKU.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| sku | text | Product SKU |
| classification | text | Product classification |
| month_of_sale | text | Month name |
| year_of_sale | integer | Year |
| total_quantity | integer | Units sold in month |
| total_revenue | numeric(12,2) | Revenue in month |
| order_count | integer | Number of orders |
| _updated_at | timestamptz | Last update time |

Unique constraint: (sku, month_of_sale, year_of_sale)

### gold.completed_sales_items_snapshot
Completed/shipped orders snapshot for reporting.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| order_id | text | Order identifier |
| sku | text | Product SKU |
| quantity | integer | Units sold |
| unit_price | numeric(10,2) | Price per unit (USD) |
| line_price | numeric(10,2) | Total line amount (USD) |
| marketplace | text | Sales channel |
| sale_date | date | Date of sale |
| sale_timestamp | timestamptz | Exact sale time |
| category | text | Product category |
| short_description | text | Product description |
| shipping_country | text | Destination country |
| shipping_region | text | Destination region/state |
| shipping_city | text | Destination city |
| item_source | text | Data source identifier |
| _snapshot_date | date | Date snapshot was built |
| part_number | text | Product part number |
| raw_unitprice_a | numeric(10,2) | Original unit price before FX |
| raw_lineprice | numeric(10,2) | Original line price before FX |

Unique constraint: (order_id, sku, item_source)

### gold.forecast_depletion
Inventory depletion forecasts per SKU.

| Column | Type | Notes |
|--------|------|-------|
| sku | text PK | Product SKU |
| classification | text | e.g. "healthy", "at_risk", "critical" |
| quantity_on_hand | integer | Current inventory |
| quantity_incoming | integer | Incoming inventory |
| months_to_depletion | numeric(5,2) | Months until stockout |
| months_to_depletion_with_incoming | numeric(5,2) | With incoming stock |
| method_used | text | Forecasting method |
| forecast_date | date | Date of forecast |
| _updated_at | timestamptz | Last update time |
| seas_mtd | numeric(5,2) | Seasonal months-to-depletion |
| imp_month_cnt | integer | Months with imputed data |
| total_12m_sales | numeric(10,2) | Trailing 12-month sales |
| insuff_data | boolean | Insufficient data flag |
| simple_mtd | numeric(5,2) | Simple months-to-depletion |
| depl_diff | numeric(5,2) | Depletion method difference |

### gold.monthly_inventory_sales
Monthly inventory and sales summary.

| Column | Type | Notes |
|--------|------|-------|
| sku | text PK (composite) | Product SKU |
| month_name | text PK (composite) | Month name |
| website_sales | integer | Website channel sales |
| amazon_sales | integer | Amazon channel sales |
| year_sales | integer | Annual sales total |
| _updated_at | timestamptz | Last update time |

## Key Data Facts

- Sales data range: 2017-12-30 to present
- ~208k line items in silver.fact_sales_items
- ~202k completed items in gold snapshot
- 142 SKUs tracked in forecast_depletion
- 3+ distinct marketplaces (Amazon, Shopify, WooCommerce are the primary channels)
- Active marketplaces: Amazon, Shopify (WooCommerce is deprecated)
- ~5,460 monthly SKU aggregation rows
- ~432 monthly inventory sales rows
