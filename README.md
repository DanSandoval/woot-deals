# Woot Deals Tracker

A Google Cloud Run service that monitors Woot.com for deals matching your keywords (e-readers, Kindle, etc.) and sends email notifications when new matching deals are found.

## Overview

This service:
- Periodically checks the Woot API for new deals
- Filters deals based on configurable keywords
- Sends email notifications for matching deals
- Tracks previously seen deals to avoid duplicates
- Runs on Google Cloud Platform's free tier

## Files

- `main.py` - The main application code
- `Dockerfile` - Container configuration for Cloud Run
- `requirements.txt` - Python dependencies
- `test_service.py` - Testing script for the deployed service
- `test_api_endpoints.py` - Script to test Woot API connectivity
- `test_pipeline.py` - Offline tests for the deal-checking pipeline (no credentials needed)

## Setup Instructions

### Prerequisites

1. A Google Cloud Platform account
2. A Woot API key (register at developer.woot.com)
3. A Gmail account with App Password configured

### Google Cloud Setup

1. Create a new Google Cloud project
2. Enable the following APIs:
   - Cloud Run API
   - Cloud Scheduler API
   - Cloud Storage API
   - Cloud Build API

3. Create a Cloud Storage bucket:
   ```
   gsutil mb -l REGION gs://YOUR-BUCKET-NAME
   ```

4. Deploy to Cloud Run:
   ```
   gcloud run deploy woot-deals \
     --source . \
     --platform managed \
     --region REGION \
     --memory 256Mi \
     --min-instances 0 \
     --max-instances 1 \
     --set-env-vars="WOOT_API_KEY=your-api-key,GMAIL_USER=your-email@gmail.com,GMAIL_APP_PASSWORD=your-app-password,EMAIL_RECIPIENT=recipient@example.com,BUCKET_NAME=your-bucket-name" \
     --allow-unauthenticated
   ```

5. Set up Cloud Scheduler:
   ```
   gcloud scheduler jobs create http woot-deals-hourly \
     --schedule="0 * * * *" \
     --uri="https://YOUR-CLOUD-RUN-URL" \
     --http-method=GET \
     --location=REGION
   ```

### Testing

After deployment, test the service using:

```
python test_service.py --url YOUR-CLOUD-RUN-URL --test all
```

To run the offline pipeline tests (feed pagination, rate-limit backoff,
keyword matching, seen-deal state, notifications):

```
python test_pipeline.py
```

To test only the API connectivity:

```
python test_api_endpoints.py --api-key YOUR-WOOT-API-KEY
```

## Configuration

Modify the `KEYWORDS` list in `main.py` to customize which products you're interested in.

## Monitoring

Check the Cloud Run logs for service activity:

```
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=woot-deals" --limit 50
```

## Monitoring

This service once ran broken for months while looking perfectly healthy: it
returned HTTP 200 every hour and reported "0 matches" while only reading a
quarter of the feed. Two independent layers now exist so that cannot repeat.

### Layer 1 - the run reports on itself

Every run ends by classifying itself:

| Status | Meaning | HTTP |
|---|---|---|
| `ok` | ran, and the results can be trusted | 200 |
| `degraded` | ran, but the results cannot be trusted | 200 |
| `failed` | could not run at all | 5xx |

`failed` returns 5xx so the Cloud Scheduler job goes red. `degraded` stays 200
deliberately: an immediate scheduler retry would just spend more of the Woot
rate-limit budget, and the alert below is the better signal.

Conditions that mark a run `degraded` or `failed`:

| Problem | Meaning |
|---|---|
| `feed_incomplete` | the feed was cut short - the original bug |
| `feed_too_small` | fewer than `FEED_SIZE_FLOOR` items came back |
| `feed_shrank` | feed is under `FEED_SHRINK_RATIO` of its recent median |
| `feed_pages_too_few` | fewer pages read than the feed advertised, yet it reported itself complete |
| `feed_schema_invalid` | `TotalPages` or `Items` missing or changed type |
| `feed_duplicate_ids` | the same offers repeating - pagination may be ignored |
| `feed_text_missing` | feed items lost their titles, so the matcher sees nothing |
| `feed_not_changing` | no new offers for `NO_NEW_ITEMS_RUNS` runs; feed stale or seen-index wrong |
| `feed_outgrowing_budget` | the feed has more pages than the run budget can read |
| `feed_empty` | the feed returned nothing |
| `detail_fetch_incomplete` | candidate offers could not be keyword-checked |
| `notification_failed` | matching deals could not be sent |
| `deal_format_failed` | an offer could not be formatted and was skipped |
| `seen_state_unreadable` | state could not be read; the run aborts rather than re-alert everything |
| `seen_state_unwritable` | state could not be saved; would otherwise re-send every deal hourly |
| `seen_state_implausible` | state reads and writes fine but holds an implausible number of entries |
| `run_near_deadline` | the run is approaching its time budget |
| `storage_client_uninitialized` | Cloud Storage was unavailable at startup |
| `missing_env_vars` | a required setting is unset |
| `unhandled_exception` | anything otherwise uncaught |

Several of these overlap deliberately. The original truncation now trips
`feed_incomplete`, `feed_too_small` and `feed_shrank` together - independent
detectors for one fault, collapsed into a single alert by the signature rule.

Two design notes worth keeping in mind when tuning:

- **`FEED_SIZE_FLOOR` must sit above the failure it catches.** The original
  truncation returned ~1300 items. A floor of 1000 would have sat silently
  through the exact bug it was added for. It is 3000.
- **Only fully healthy runs move the baseline.** `feed_shrank` compares against
  the median of recent complete, healthy runs. If truncated runs fed the
  baseline it would drift down to meet the failure and quietly disarm itself -
  which is the same self-concealing property that made the original bug
  invisible.

A problem sends a text and an email. To avoid alert fatigue, an alert fires when
a problem first appears and then at most once per `ALERT_COOLDOWN_HOURS` while
the same problem persists. A different problem alerts immediately. When things
recover, a single "recovered" message is sent.

### The canary (not yet armed)

`CANARY_KEYWORD` counts feed items mentioning a term Woot always has live, using
text already fetched - no extra API calls. It exercises the real matching
pipeline every hour with a guaranteed-positive case, so a matcher regression
shows up within hours instead of whenever a Kindle next happens to go on sale.

It is **observe-only**: `canary_hits` appears in the health line but nothing
alerts on it. Before arming it, confirm the term really does appear in every run:

```
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="woot-deals" AND textPayload=~"WOOT_HEALTH"' --project=woot-deals-tracker --limit=200 --format='value(textPayload)' | grep -o 'canary_hits=[0-9]*' | sort | uniq -c
```

If the minimum over a week is comfortably above zero, alert when it hits zero on
two consecutive complete runs.

### Known gap

Both layers live in the same GCP project as the service. If the project itself
is disabled or billing lapses, the watchdog dies with the thing it watches. A
dead-man's switch on an external service (healthchecks.io, cronitor) pinged only
on `status=ok` would close that gap; it needs a third-party account, so it has
not been set up.

### Layer 2 - Cloud Monitoring watchdog

Layer 1 cannot report a failure the service is not awake to notice, and it
cannot email about a broken email path. Two alert policies in the
`woot-deals-tracker` project cover that, both notifying `casadtd@gmail.com`:

- **Woot tracker reported a problem** - fires on the log-based metric
  `woot_health_problem`, which counts `WOOT_HEALTH status=degraded|failed` lines.
- **Woot tracker has not completed a healthy run** - fires when no
  `WOOT_HEALTH status=ok` line has appeared for 3 hours. This is the one that
  catches the service not running at all.

Every run emits exactly one machine-readable line that these key off:

```
WOOT_HEALTH status=ok problems=none feed_complete=true feed_items=5000 matches=0 ...
```

To see recent health lines:

```
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="woot-deals" AND textPayload=~"WOOT_HEALTH"' --project=woot-deals-tracker --limit=20 --format='value(timestamp,textPayload)'
```

To list the alerting setup:

```
gcloud alpha monitoring policies list --project=woot-deals-tracker --format='table(displayName,enabled)'
gcloud logging metrics list --project=woot-deals-tracker
gcloud beta monitoring channels list --project=woot-deals-tracker
```

## Troubleshooting

If the service isn't working as expected:

1. Check if all environment variables are set correctly
2. Verify the Cloud Storage bucket exists and is accessible
3. Test the API connectivity using `test_api_endpoints.py`
4. Check the logs for detailed error messages

### Rate limiting

The Woot API rate limits like a token bucket: a small burst, then roughly one
request per second. The feed is ~51 pages, so every API call goes through a
shared pacer (`MIN_REQUEST_INTERVAL`) and retries 429s with exponential backoff.

If the logs show `complete=False` in the run summary, the feed was cut short and
the offers on the pages that were never reached are deliberately left unrecorded
so the next run retries them. Persistent `complete=False` means the pacing needs
to be slower, or the run does not fit inside the Cloud Scheduler attempt deadline.