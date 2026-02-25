# Bid Scraper Behavioral Specification

## Scenario 1: Database connectivity
GIVEN the bid scraper service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries for all documented tables.

## Scenario 2: Opportunity volume
GIVEN the bid scraper ingests open and past opportunities from Hillsborough County.
WHEN the bid_opportunities table total row count is computed.
THEN there are at least 500 total opportunity records.

## Scenario 3: Contract volume
GIVEN the bid scraper captures awarded contracts from Hillsborough County.
WHEN the public_contracts table total row count is computed.
THEN there are at least 500 total contract records.

## Scenario 4: Scrape freshness
GIVEN the bid scraper runs on a daily schedule.
WHEN the most recent scrape_runs entry is examined.
THEN the most recent run with status "success" has a finished_at timestamp within the past 72 hours.

## Scenario 5: Scrape success rate
GIVEN the bid scraper should succeed on most runs.
WHEN the scrape_runs entries over the last 14 days are analyzed.
THEN at least 80% of completed runs have status "success".

## Scenario 6: Required fields integrity
GIVEN procurement records are ingested from external portals.
WHEN the bid_opportunities table is analyzed for null required fields.
THEN at least 95% of rows have non-null project_id, project_name, and source fields.

## Scenario 7: Contract date validity
GIVEN contracts have start and end date fields.
WHEN the public_contracts table is analyzed for date consistency.
THEN zero contracts have a start_date after their end_date.

## Scenario 8: Data freshness -- records updated recently
GIVEN the scraper updates records on each run.
WHEN the last_updated timestamps in bid_opportunities are analyzed.
THEN records have been updated within the past 72 hours.

## Scenario 9: Vendor completeness
GIVEN the bid scraper captures vendor records alongside contracts.
WHEN the vendors table is analyzed.
THEN there are at least 500 vendor records, all with non-null vendor_name fields.

---

# Database Schema Reference

Connection details are in your environment variables (PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD).

```
psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE
```

## Tables

### bid_opportunities
Procurement listings scraped from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| project_id | text PK | Bonfire project ID |
| reference_id | text | Bonfire reference ID |
| project_name | text | Listing title |
| status_id | text | Bonfire status code |
| sub_status_id | text | Sub-status |
| department_id | text | Department |
| close_date | timestamptz | Bid deadline |
| source | text | "openOpportunities" or "pastOpportunities" |
| content_hash | text | Hash for change detection |
| first_seen_at | timestamptz | When scraper first found it |
| last_updated | timestamptz | Last update time |
| raw_data | jsonb | Full API response |

### public_contracts
Awarded contracts from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| contract_id | text PK | Bonfire contract ID |
| name | text | Contract title |
| vendor_id | text | Vendor reference |
| vendor_name | text | Vendor name |
| department_id | text | Department |
| organization_id | text | Organization |
| contract_status_id | text | Contract status code |
| is_extendable | boolean | Whether contract is extendable |
| start_date | timestamptz | Contract start |
| end_date | timestamptz | Contract end |
| content_hash | text | Hash for change detection |
| first_seen_at | timestamptz | When scraper first found it |
| last_updated | timestamptz | Last update time |
| raw_data | jsonb | Full API response |

### vendors
Vendor records from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| vendor_id | text PK | Bonfire vendor ID |
| vendor_name | text | Vendor company name |
| first_seen_at | timestamptz | When scraper first found it |
| last_updated | timestamptz | Last update time |
| raw_data | jsonb | Full API response |

### document_takers
Companies that downloaded bid documents for an opportunity.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| project_id | text FK->bid_opportunities | Which opportunity |
| vendor_name | text | Company that took documents |
| first_seen_at | timestamptz | When first seen |
| last_updated | timestamptz | Last update time |

Unique constraint: (project_id, vendor_name)

### scrape_runs
Execution log for each scrape run.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| started_at | timestamptz | Run start time |
| finished_at | timestamptz | Run end time |
| status | text | "running", "success", "failed" |
| phase | text | Which scrape phase |
| records_processed | integer | Count processed |
| records_new | integer | Count new records |
| records_updated | integer | Count updated records |
| error_message | text | Error details if failed |
| summary | jsonb | Run summary data |

### contract_opportunity_map
Links contracts to opportunities they were awarded from.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| contract_id | text | Contract reference |
| opportunity_project_id | text FK->bid_opportunities | Opportunity reference |
| match_method | match_method | How the match was determined |
| confidence | real | Match confidence 0.0-1.0 |
| created_at | timestamptz | When mapping was created |

Unique constraint: (contract_id, opportunity_project_id, match_method)
