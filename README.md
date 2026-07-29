# Designing a Data Extraction and Validation Workflow

## Overview

This project demonstrates an end-to-end data engineering workflow using
Python to collect, process, validate, analyze, and share data. The
project scrapes recent articles from Dazed Digital, keeping only
articles published within the last six months and exporting the results
as structured JSON.

Presentation Link:
https://app.notion.com/p/Scraper-Presentation-3ace2d27cfcc80638d63ef80d6731dfe?source=copy_link

## Workflow

### 1. Data Extraction

-   Uses Playwright to automate browser interactions.
-   Visits Fashion, Film & TV, Music, and Life & Culture.
-   Collects Latest and Trending article links.
-   Applies asynchronous processing for improved performance.
-   Filters out articles older than six months.

### 2. Data Processing

-   Cleans whitespace and Unicode characters.
-   Parses publication dates into ISO-8601 format.
-   Validates article URLs.
-   Removes duplicate records.
-   Stores standardized results in JSON.

### 3. SQL Analysis

After loading the JSON into SQLite or PostgreSQL:

``` sql
SELECT section, COUNT(*) FROM articles GROUP BY section;
SELECT bucket, COUNT(*) FROM articles GROUP BY bucket;
SELECT date_posted, COUNT(*) FROM articles GROUP BY date_posted;
SELECT url, COUNT(*) FROM articles GROUP BY url HAVING COUNT(*)>1;
```

Possible analyses: - Articles per section - Trending vs Latest
distribution - Publication trends - Duplicate detection - Weekly
publishing activity

### 4. Data Validation

-   Verify required fields exist.
-   Validate URL format.
-   Ensure dates are valid ISO-8601.
-   Confirm all articles are within six months.
-   Check duplicate URLs.
-   Log rejected records.

### 5. Troubleshooting

-   Retry failed requests.
-   Handle timeout exceptions.
-   Detect website layout changes.
-   Log scraping failures.
-   Unit test date parsing and validation.

### 6. Sharing Results

-   Export JSON, CSV, or SQL tables.
-   Build dashboards in Tableau or Power BI.
-   Generate automated reports.
-   Schedule with cron or GitHub Actions.

## Technologies

-   Python
-   Playwright
-   asyncio
-   JSON
-   SQL (SQLite/PostgreSQL)
-   Git

## Key Takeaways

This workflow demonstrates: - Automated data extraction - Reliable data
transformation - SQL-based analytics - Data quality validation - Error
handling - Scalable reporting
