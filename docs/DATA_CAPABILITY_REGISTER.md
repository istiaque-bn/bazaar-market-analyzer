# Stock Bazaar data capability register

This register distinguishes data that is currently implemented from data that requires a licensed provider, official exchange agreement or approved source connection. A data field is never represented as available before it has a verified provenance path.

| Capability | Current status | Notes |
| --- | --- | --- |
| Historical-price ingestion | Implemented | DSE/CSE historical ingestion paths store OHLCV rows and import batches. |
| Source timestamps and provenance | Implemented | Every new row records source, fetch time, raw/adjustment state and import batch; revisions are retained. |
| Data-quality validation | Implemented | OHLC validation, abnormal jumps, repeated quotes and session gaps are flagged without deleting evidence. |
| Missing/stale-data detection | Implemented | Per-exchange freshness, failed import batches and stale analysis/price alerts are monitored. |
| Adjusted historical prices | Not connected | Existing rows are explicitly marked raw/unknown. Corporate-action data is required before adjustment. |
| Halted/suspended shares | Not connected | Repeated/no-trade prices are only a quality signal, not an official halt determination. Requires exchange status feed. |
| Licensed intraday prices / market depth | Not connected | Requires an approved real-time/depth licence and credentials. |
| Corporate announcements, financials, shareholding, IPO, AGM/EGM, record dates, block trades, spot market | Not connected | Requires an approved official/provider feed, terms and a schema mapping. |
| News | Not connected | Requires a licensed or permitted news source and redistribution terms. |

## Integration rules

For each future source, add the written permission/licence record, source URL or API identity, retrieval time, payload identifier/checksum where permitted, field mapping, retention rule and validation tests. Keep credentials outside the repository and do not substitute scraped/unlicensed data for a licensed feed.
