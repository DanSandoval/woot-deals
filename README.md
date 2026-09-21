# Woot Deals Tracker

A Google Cloud Run service that monitors Woot.com for deals matching your keywords (e-readers, Kindle, etc.) and sends email notifications when new matching deals are found.

## Overview

This service:
- Polls all 11 Woot feeds every 30 minutes and merges them into one
  deduplicated catalogue (~13,600 offers), because the `All` feed alone is
  capped at 5000 items and shows only ~40% of what is for sale
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
   gcloud scheduler jobs create http woot-deals-tracker \
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

Health events come in two tiers. **Paging** events mark the run `degraded` or
`failed`, which is what the alert policy counts, and can reach the user as an
email and text. **Notable** events are recorded and logged, and appear in the
summary line's `notes=` field, but leave the run `ok` and never alert.

The split exists because it once did not. Every one of these conditions used to
escalate the run, so a harmless one - the seen index drifting past an ageing
size ceiling - reported a problem every 30 minutes for a day in September 2026.
The cost of that is not the annoyance: a real failure arriving in the middle of
it would have been invisible.

The test for the notable tier is "what would I do about it at 3am?". If the
answer is nothing, it does not page.

Paging - the tracker is broken, blind, or would spam you:

| Problem | Meaning |
|---|---|
| `feed_incomplete` | the feed was cut short - the original bug |
| `feed_too_small` | fewer than `FEED_SIZE_FLOOR` items came back |
| `feed_coverage_partial` | fewer than all 11 feeds were read, so the catalogue seen is incomplete |
| `feed_schema_invalid` | `TotalPages` or `Items` missing or changed type |
| `feed_text_missing` | feed items lost their titles, so the matcher sees nothing |
| `feed_not_changing` | no new offers for `NO_NEW_ITEMS_HOURS`; feed stale or seen-index wrong |
| `feed_empty` | the feed returned nothing |
| `notification_failed` | matching deals could not be sent |
| `seen_state_unreadable` | state could not be read; the run aborts rather than re-alert everything |
| `seen_state_unwritable` | state could not be saved; would otherwise re-send every deal every run |
| `seen_state_implausible` | the index is under `SEEN_STATE_MIN`; truncated state re-notifies every live deal |
| `storage_client_uninitialized` | Cloud Storage was unavailable at startup |
| `missing_env_vars` | a required setting is unset |
| `unhandled_exception` | anything otherwise uncaught |

`feed_text_missing` and `feed_not_changing` are deliberately in this tier. Both
describe the pipeline looking healthy while observing or matching nothing, which
is the exact failure this service was written after, and `canary_hits` is only
observed and never checked - so these two are the only guards against it.

Notable - context for whoever is already looking, listed in `NOTABLE_EVENTS`:

| Note | Meaning |
|---|---|
| `feed_newly_capped` | a feed newly hit Woot's 5000-item ceiling; offers past it are unreachable |
| `feed_shrank` | feed is under `FEED_SHRINK_RATIO` of its recent median |
| `feed_fallback_skipped` | a feed needed the expensive paginated retry and the day's request budget could not afford it |
| `feed_outgrowing_budget` | a feed reports more pages than the paginated fallback could read |
| `detail_fetch_incomplete` | candidate offers could not be keyword-checked |
| `deal_format_failed` | an offer could not be formatted and was skipped |
| `seen_state_oversized` | the index passed `SEEN_STATE_MAX`, a loose runaway backstop |
| `seen_state_unpruned` | entries older than the retention window survived the prune, so retention has stopped working |
| `run_near_deadline` | the run is approaching its time budget |

An event kind that is not named in `NOTABLE_EVENTS` pages. That default is
deliberate: forgetting to classify a new check should over-alert, never
silently disable it.

Several of these overlap deliberately. The original truncation now trips
`feed_incomplete` and `feed_too_small` in the paging tier and notes
`feed_shrank` alongside them - independent detectors for one fault, collapsed
into a single alert by the signature rule. The overlap is why `feed_shrank` can
sit in the notable tier without losing coverage: it is a relative dip, and when
the cause is structural the two paging checks fire with it.

Two design notes worth keeping in mind when tuning:

- **`FEED_SIZE_FLOOR` must sit above the failure it catches.** The original
  truncation returned ~1300 items. A floor of 1000 would have sat silently
  through the exact bug it was added for. It is 6000, chosen to sit above the
  5000 that the `All` feed alone returns.
- **Only fully healthy runs move the baseline.** `feed_shrank` compares against
  the median of recent complete, healthy runs. If truncated runs fed the
  baseline it would drift down to meet the failure and quietly disarm itself -
  which is the same self-concealing property that made the original bug
  invisible.

A problem sends a text and an email. To avoid alert fatigue, an alert fires when
a problem first appears and then at most once per `ALERT_COOLDOWN_HOURS` while
the same problem persists. A different problem alerts immediately. When things
recover, a single "recovered" message is sent.

Two consequences worth knowing before tuning anything here:

- **The cooldown decouples texts from log noise.** When `seen_state_implausible`
  fired on all 48 runs of 2026-09-20, it produced 3 texts, not 48. Counting
  `WOOT_HEALTH_EVENT` lines in the logs badly overstates what the user received;
  count `Health alert sent` instead.
- **Every incident costs two messages** - the problem, then the matching
  "recovered". A transient blip that clears by itself still sends both. This is
  deliberate (silence is ambiguous) but it does double the volume, and it is the
  obvious thing to change if alerts still feel too frequent.

### The canary (not yet armed)

`CANARY_KEYWORD` counts feed items mentioning a term Woot always has live, using
text already fetched - no extra API calls. It exercises the real matching
pipeline every run with a guaranteed-positive case, so a matcher regression
shows up within hours instead of whenever a Kindle next happens to go on sale.

It is **observe-only**: `canary_hits` appears in the health line but nothing
alerts on it. **This is the service's largest remaining blind spot.** A bug in
the matching logic itself - titles present, offers flowing, but `matched_keywords`
silently returning nothing - fires no check: `feed_text_missing` sees titles and
`feed_not_changing` sees new offers. Those two only guard the problem
indirectly, which is why neither may be demoted to the notable tier.

Readings have run 94-102 over the last month. Before arming it, confirm the term
really does appear in every run:

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
`woot-deals-tracker` project cover that, both notifying the project owner:

- **Woot tracker reported a problem** - fires on the log-based metric
  `woot_health_problem`, which counts `WOOT_HEALTH status=degraded|failed` lines.
- **Woot tracker has not completed a healthy run** - fires when no
  `WOOT_HEALTH status=ok` line has appeared for 3 hours. This is the one that
  catches the service not running at all.

Every run emits exactly one machine-readable line that these key off:

```
WOOT_HEALTH status=ok problems=none canary_hits=102 capped=All deferred=0
duration_s=20.8 feed_complete=true feed_items=9124 feeds=11/11 matches=0
new_items=0 notified=false pages=0 quota_used=352/1000 raw_items=17033
rate_limit_hits=0 requests=11 seen_deals=90797
```

`problems=` lists paging events only. When a notable event fires, a `notes=`
field appears after it and the status stays `ok`:

```
WOOT_HEALTH status=ok problems=none notes=feed_newly_capped capped=All,Home ...
```

That the status stays `ok` is load-bearing, not cosmetic. The absence policy
below watches for the literal string `status=ok`, so an event that suppressed it
on every run would eventually fire "has not completed a healthy run" - which
means the service is dead - in place of the harmless alert being removed.

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

## Feed coverage and the 5000-item cap

Woot caps **every** feed at 5000 items. This is not a response-size limit: for a
capped feed `TotalPages` reports 51, pages 1-50 each serve 100 items, and page 51
returns 404, so pagination cannot reach past it either. Verified 2026-08-31.

Measured that day, `All`, `Clearance`, `Home` and `Sports` were all at or near the
ceiling while `Electronics` (~19%) and `Computers` (~17%) had plenty of room.
Since this tracker's e-reader, AirTag and Mac mini keywords live in the uncapped
feeds, the hidden inventory is mostly home goods. (Which feed 3D printers land in
has not been measured; if it is `Home` or `Tools` near the ceiling, some can be
hidden.) **That is the only
reason the cap is tolerable** - if `Electronics` or `Computers` ever approach 5000,
real deals start being hidden, which is what `feed_newly_capped` exists to catch.

There is no way around it inside the API. Verified dead ends:

- paginating past page 50 -> 404
- undocumented feed names (`Sellout`, `Daily`, `Grocery`, sub-categories) -> 400
- no `sort`, `since`, `modifiedAfter` or filter parameter exists

Merging the 11 documented feeds is the whole of what is reachable. Raising the cap
would need Woot to agree; the venue is their API discussion forum thread.

## Deploying

**Pushing to `master` deploys to production.** A Cloud Build trigger
(`^master$`) builds the Dockerfile and runs `gcloud run deploy`. It passes only
`--image`, `--labels` and `--region`, so Cloud Run inherits the existing service
config - the `maxScale=1` / `containerConcurrency=1` settings and all env vars
survive a trigger deploy (verified on revision 00037).

Those two settings matter: all per-run state in `main.py` is module-level globals
with no lock, so two overlapping runs would corrupt each other's health reporting
and double the request rate against the shared API key. Keep them at 1.

To deploy by hand without going through `master`:

```
gcloud run deploy woot-deals --source . --region=us-central1 --project=woot-deals-tracker
```

## Troubleshooting

If the service isn't working as expected:

1. Check if all environment variables are set correctly
2. Verify the Cloud Storage bucket exists and is accessible
3. Test the API connectivity using `test_api_endpoints.py`
4. Check the logs for detailed error messages

### Rate limiting

The Woot API enforces **two separate limits**, and telling them apart matters:

| 429 response | Meaning | Does backoff help? |
|---|---|---|
| `ThrottlingException` / "Too Many Requests" | the 1/sec rate throttle | yes |
| `LimitExceededException` / "Limit Exceeded" | the **1000 requests/day quota** | no - only 00:00 UTC does |

Every call goes through a shared pacer (`MIN_REQUEST_INTERVAL`) and retries the
first kind with exponential backoff. The second kind took the service down for an
evening: paginating one feed cost 51 requests, so hourly polling needed 1224/day
against the 1000/day quota and the feed died every evening around 19:00 UTC.

Omitting the `page` parameter returns a whole feed in ONE request, so a run now
costs 11 requests (one per feed) rather than 51 per feed. Daily spend is tracked
across runs in `health_state.json`, keyed by UTC date because that is when the
quota resets, and reported as `quota_used=` on every run.

If the logs show `complete=False` in the run summary, the feed was cut short and
the offers on the pages that were never reached are deliberately left unrecorded
so the next run retries them. Persistent `complete=False` means the pacing needs
to be slower, or the run does not fit inside the Cloud Scheduler attempt deadline.