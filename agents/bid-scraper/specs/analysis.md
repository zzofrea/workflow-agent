# Bid Scraper Analysis Instructions

Produce a concise markdown report covering the following areas.

## Recent Activity
- Count of new opportunities added in the last 7 days
- Count of new contracts added in the last 7 days
- Any changes in scrape success rate

## Upcoming Deadlines
- List opportunities with close_date in the next 14 days
- Include project_name, close_date, and source

## Competitive Intelligence
- Query the burgess_civil_intel view for any new Burgess Civil activity
- Summarize contracts won and document-taking activity

## Data Quality
- Percentage of bid_opportunities with null required fields
- Any scrape failures in the last 7 days

## Patterns and Anomalies
- Note any unusual volume changes (spikes or drops in opportunities/contracts)
- Flag any departments with significantly more activity than usual

Keep the report concise. Use tables where appropriate. Include actual numbers
from your queries as evidence.
