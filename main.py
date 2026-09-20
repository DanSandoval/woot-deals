import requests
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
import os
from google.cloud import storage
import sys
import traceback
import time
from flask import Flask, request
import random

# Set up detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Configuration (use environment variables for sensitive data)
WOOT_API_KEY = os.environ.get("WOOT_API_KEY")
FEED_BASE = "https://developer.woot.com/feed"
FEED_ENDPOINT = f"{FEED_BASE}/All"  # kept for the connectivity self-tests

# The All feed is capped at 5000 items by Woot, but the catalog is ~12400, so
# All alone shows about 40% of it. The per-category feeds together are a strict
# superset -- measured: every offer in All also appears in some category, while
# 7445 offers appear ONLY in a category. Clearance and Sellout in particular are
# where discounted e-readers land, so polling All alone can hide exactly the
# deals this tracker exists to find.
FEED_NAMES = ["All", "Clearance", "Computers", "Electronics", "Featured",
              "Home", "Gourmet", "Shirts", "Sports", "Tools", "Wootoff"]

# The API serves 100 items per page and does not let you change it. Used to spot
# a non-paginated request that came back paginated anyway.
FEED_PAGE_SIZE = 100

GETOFFERS_ENDPOINT = "https://developer.woot.com/getoffers"
KEYWORDS = ["kindle", "ereader", "e-reader", "e-ink", "kobo", "nook", "eink",
            "airtag", "air-tag", "mac mini", "3d printer", "3-d printer"]
# Both AirTag spellings are listed for the same reason as ereader/e-reader:
# normalize_text flattens hyphens, so "air-tag" covers "Air Tag" and
# "Air-Tag" while "airtag" covers Apple's own one-word branding. Matching is
# substring, so "airtag" also picks up the "AirTags" plural on its own.
# "mac mini" needs one entry: flattening already turns "Mac-Mini" and the
# "apple-mac-mini-m4" slug into the same text. "3d printer" covers "3D Printer",
# "3D-Printer" and the plural, but "3-D Printer" flattens to "3 d printer", so
# that spelling is listed separately. Accessories named after the product
# ("Stand for Mac mini", "3D Printer Filament") match too, as AirTag cases do.


def normalize_text(text):
    """Lowercase and flatten hyphens so "e-reader", "e reader" and slug text all match."""
    return text.replace("-", " ").lower()


NORMALIZED_KEYWORDS = [normalize_text(k) for k in KEYWORDS]


def matched_keywords(text):
    """Return the keywords present in a piece of text (empty when text is missing or not a string)."""
    if not isinstance(text, str) or not text:
        return []
    haystack = normalize_text(text)
    return [KEYWORDS[i] for i, k in enumerate(NORMALIZED_KEYWORDS) if k in haystack]

# Gmail configuration
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

# GCS configuration
BUCKET_NAME = os.environ.get("BUCKET_NAME")
SEEN_DEALS_FILENAME = "seen_deals.json"

# Rate limiting configuration
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 5  # seconds
MAX_RETRY_DELAY = 60  # seconds
REQUEST_TIMEOUT = 30  # seconds

# The Woot API rate limits like a token bucket: a small burst, then roughly one
# request per second. Every call goes through _throttle() so feed pagination and
# getoffers batches draw on one shared pacer instead of racing each other.
MIN_REQUEST_INTERVAL = 1.25  # seconds between any two Woot API requests
DETAIL_BATCH_SIZE = 10  # offer IDs per getoffers call

# The API also enforces a hard 1000 requests/day that resets at 00:00 UTC
# (documented at developer.woot.com). This is a separate limit from the rate
# above, and pacing cannot help once it is gone: the day is simply over. It went
# unnoticed for months because polling 51 pages hourly needs 1224/day -- 22% over
# -- so the feed died every evening and recovered by itself at midnight.
WOOT_DAILY_QUOTA = 1000
# Stop well short of the real ceiling. The gap absorbs retries and leaves the
# later runs of the day enough budget to still fetch offer details.
DAILY_REQUEST_CEILING = 800
# What one full paginated crawl costs: ~51 pages plus retries. Used to decide
# whether the day can still afford the fallback path.
PAGINATED_FETCH_COST = 55

# Cloud Scheduler gives this service a limited attempt deadline. Staying inside
# it matters: an overrunning request gets retried by the scheduler, which would
# only burn more rate-limit budget.
RUN_BUDGET_SECONDS = 150
FEED_BUDGET_RESERVE = 45  # keep this much of the budget for the detail fetch
MAX_FEED_PAGES = 200  # guard against a runaway TotalPages value

# An offer that has not appeared in the feed for this long is dropped from the
# seen-deals index, which bounds the state file instead of growing it forever.
SEEN_DEALS_RETENTION_DAYS = 45

# --- Health monitoring -------------------------------------------------------
# This service failed silently for months: it kept returning HTTP 200 and kept
# reporting "0 matches" while only reading a quarter of the feed. Everything
# below exists so that cannot happen again without someone being told.
HEALTH_STATE_FILENAME = "health_state.json"

# Every run emits one machine-readable summary line starting with this marker.
# Cloud Monitoring alerts on it: a log-based metric counts status=failed/degraded,
# and an absence policy fires when no status=ok line appears for a few hours,
# which is the only way to catch the service not running at all.
HEALTH_MARKER = "WOOT_HEALTH"

# Not everything worth recording is worth interrupting someone for. Every check
# below used to escalate the run to "degraded", which is what the paging metric
# counts, so twenty-odd conditions ranging from "the service is down" to "a Woot
# category got big" all rang the same bell. One of the harmless ones then fired
# every thirty minutes for a day, which is how a noisy channel actually fails:
# a real problem arriving in the middle of that is invisible.
#
# Events named here are recorded, logged and reported in the summary line's
# notes= field, but do not change the run's status and do not alert. Everything
# NOT named here pages -- an unclassified new event is meant to be loud, since
# forgetting to classify one should never silently disable it.
#
# The test for this list is "what would I do about it at 3am?". Nothing here has
# an answer; each is context for a human already looking. In particular
# feed_not_changing and feed_text_missing are deliberately ABSENT: both describe
# the pipeline looking healthy while observing or matching nothing, which is the
# exact failure this service was built after, and canary_hits is only observed,
# never checked, so those two are the only guards against it.
NOTABLE_EVENTS = frozenset({
    "feed_newly_capped",       # Woot's inventory crossed a line; nothing to do
    "feed_fallback_skipped",   # the paginated fallback was not needed or afforded
    "feed_shrank",             # relative dip; feed_coverage_partial pages instead
    "feed_outgrowing_budget",  # early warning, not a failure
    "run_near_deadline",       # ditto; an actual timeout fails the run loudly
    "detail_fetch_incomplete", # some detail lookups missed; matching still ran
    "deal_format_failed",      # one offer rendered badly
    "seen_state_oversized",    # growth backstop, not a malfunction
    "seen_state_unpruned",     # retention is not dropping anything; real but slow
})

# The floor has to sit ABOVE the failure it exists to catch: the original
# truncation returned ~1300 items, so a floor of 1000 would have sat silently
# through the very bug it was added for. 6000 sits just above the 5000 that the
# All feed alone returns, so silently regressing to All-only coverage -- the most
# likely way this breaks -- trips it instead of looking healthy.
#
# This is an absolute number describing a catalogue that changes size, so it is
# worth knowing what backs it up. feed_coverage_partial now asks the structural
# question directly (did we read all eleven feeds?) and pages on its own, so the
# floor is a second line rather than the only one. The merged catalogue ran
# ~13300 in early September and fell to ~9100 by the 20th, where it levelled off;
# at that size the floor still has ~34% of room. If Woot's inventory ever does
# fall through it, the honest fix is to delete this check rather than pick a new
# number -- feed_coverage_partial and FEED_SHRINK_RATIO already cover it.
FEED_SIZE_FLOOR = 6000

# Woot caps every feed at 5000 items: staff-confirmed, and page 51 answers 404,
# so pagination cannot reach past it either. A feed sitting at the ceiling is
# hiding an unknown amount of inventory, and nothing in a run says so -- it still
# reports 11/11 feeds read and complete=true. Home, Clearance and All are already
# there. What matters is the ones that are NOT: Electronics and Computers sit near
# 20% and are where this tracker's keywords actually live, so those crossing the
# ceiling is the only case that can silently cost a real deal.
WOOT_FEED_ITEM_CAP = 5000
FEED_CAP_WARN_RATIO = 0.90  # enter the capped set approaching the ceiling
# Leaving the set needs a LOWER threshold than entering it. Without that gap a
# feed sitting at the warn line crosses it back and forth on ordinary churn and
# re-reports itself as newly capped every time: Home did exactly that four times
# in ten days. Entry at 4500, exit only below 4250.
FEED_CAP_CLEAR_RATIO = 0.85

# A drop against the recent norm catches a shrink that never crosses the floor.
# The baseline is the median of recent healthy runs, not the largest ever seen: a
# high-water mark only ratchets up, so one anomalous run raises the bar forever,
# and a feed that silently serves page 1 fifty times would inflate it and then
# make the eventual fix look like a regression.
FEED_SHRINK_RATIO = 0.70
FEED_BASELINE_RUNS = 24        # ~1 day of hourly runs
FEED_BASELINE_MIN_SAMPLES = 6  # below this the ratio check is not evaluated

# Contract checks on the feed's shape. These catch the case where every pipe
# works and the content is wrong -- the class the original bug belonged to.
FEED_MIN_TITLE_RATIO = 0.95     # feed items carrying usable title text

# The seen-index holds the live catalogue plus everything still inside the
# retention window, so its size tracks catalogue x churn x retention. A tight
# absolute ceiling encodes whatever those happened to be the day it was written:
# 90000 was set when this service read one 5000-item feed, and the move to all
# eleven tripled the growth rate without anyone revisiting it. It was breached on
# 2026-09-19 and then reported a problem on every run for a day while nothing was
# actually wrong. MIN still earns its place -- a truncated index re-notifies
# every live deal -- but MAX is now only a runaway backstop, far above any
# plausible steady state, and the real question (is pruning working?) is asked
# directly below instead of inferred from a number.
SEEN_STATE_MIN = 500
SEEN_STATE_MAX = 250000

# Warn while there is still headroom, rather than after the budget binds.
RUN_DURATION_WARN_RATIO = 0.87
FEED_PAGES_WARN = 65  # at ~1.25s/page the budget supports ~84

# A term Woot always has live, matched against feed text we have already
# fetched. It exercises the real matching pipeline every hour with a
# guaranteed-positive case, so a broken matcher shows up in hours rather than
# whenever a Kindle next happens to go on sale.
# OBSERVE ONLY: recorded in the health line, not alerted on, until a week of
# data confirms it is genuinely present in every run. See README.
CANARY_KEYWORD = "refurbished"

# Do not text the user hourly about a failure they already know about: alert on
# the transition into a problem, then at most once per cooldown while it lasts.
ALERT_COOLDOWN_HOURS = 12

# Woot's catalogue turns over daily, so a full day of hourly runs seeing nothing
# new means the feed is stale or the seen-index is wrong -- the pipeline looking
# alive while no longer actually observing anything, which is how the original
# bug presented. A single run with no new offers is perfectly normal.
NO_NEW_ITEMS_RUNS = 24

# Backstops against a bug in the alerter itself: never let one logic error turn
# into unbounded texts.
MAX_REPEAT_ALERTS_PER_INCIDENT = 4
MAX_ALERTS_PER_DAY = 4

# Initialize storage client
storage_client = None
try:
    storage_client = storage.Client()
    logging.info("Successfully initialized storage client")
except Exception as e:
    logging.error(f"Error initializing storage client: {e}")
    logging.error(traceback.format_exc())

# Create Flask app
app = Flask(__name__)

# Shared request pacing and per-run budget state
_last_request_time = 0.0
_run_deadline = None

# Woot API requests made during this run, counted against the daily quota.
_request_count = 0

# Requests earlier runs already spent today, read once at the start of a run.
_quota_prior = 0

# Problems recorded during the current run, drained by report_run_health()
_health_events = []

# Observations about the current run's feed fetch, for the health checks
_feed_stats = {}

# Whether this run already spent its Woot API budget on the feed. A retry after
# that point costs another full pagination, so a late crash must not ask for one.
_feed_was_fetched = False

def test_environment_variables():
    """Test if all required environment variables are set."""
    logging.info("=== TESTING ENVIRONMENT VARIABLES ===")
    
    required_vars = {
        "WOOT_API_KEY": WOOT_API_KEY,
        "GMAIL_USER": GMAIL_USER,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
        "EMAIL_RECIPIENT": EMAIL_RECIPIENT,
        "BUCKET_NAME": BUCKET_NAME
    }
    
    all_set = True
    for name, value in required_vars.items():
        if not value:
            logging.error(f"Environment variable {name} is not set")
            all_set = False
        else:
            # Log the first and last few characters of sensitive values
            if name in ["WOOT_API_KEY", "GMAIL_APP_PASSWORD"]:
                masked_value = f"{value[:3]}...{value[-3:]}" if len(value) > 6 else "***"
                logging.info(f"{name} is set: {masked_value}")
            else:
                logging.info(f"{name} is set: {value}")
                
    if all_set:
        logging.info("All required environment variables are set")
    else:
        logging.error("Some required environment variables are missing")
    
    return all_set

def test_storage_access():
    """Test access to Cloud Storage."""
    logging.info("=== TESTING CLOUD STORAGE ACCESS ===")
    
    if not storage_client:
        logging.error("Storage client initialization failed")
        return False
    
    try:
        # Check if the bucket exists
        bucket = storage_client.bucket(BUCKET_NAME)
        exists = bucket.exists()
        
        if exists:
            logging.info(f"Bucket {BUCKET_NAME} exists")
            
            # Test writing to the bucket
            test_blob = bucket.blob("test_access.txt")
            test_blob.upload_from_string(f"Test access at {datetime.now().isoformat()}")
            logging.info("Successfully wrote test file to bucket")
            
            # Test reading from the bucket
            content = test_blob.download_as_text()
            logging.info(f"Successfully read test file from bucket: {content}")
            
            # Clean up
            test_blob.delete()
            logging.info("Successfully deleted test file from bucket")
            
            return True
        else:
            logging.error(f"Bucket {BUCKET_NAME} does not exist")
            return False
    except Exception as e:
        logging.error(f"Error testing storage access: {e}")
        logging.error(traceback.format_exc())
        return False

def test_woot_api():
    """Test connection to Woot API."""
    logging.info("=== TESTING WOOT API CONNECTION ===")
    
    if not WOOT_API_KEY:
        logging.error("WOOT_API_KEY is not set")
        return False
    
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json"
    }
    
    try:
        # Test the feed endpoint
        logging.info(f"Testing connection to feed endpoint: {FEED_ENDPOINT}")
        response = requests.get(FEED_ENDPOINT, headers=headers)
        
        if response.status_code == 200:
            api_response = response.json()
            logging.info(f"Successfully connected to feed endpoint. Received response data.")
            
            # Log details about the response structure
            logging.info(f"Response type: {type(api_response)}")
            
            if isinstance(api_response, dict):
                logging.info(f"API returned dictionary with keys: {list(api_response.keys())}")
                # Log the first level of the response to understand the structure
                for key, value in api_response.items():
                    value_type = type(value)
                    if isinstance(value, (list, dict)):
                        size_info = f" with {len(value)} items" if hasattr(value, "__len__") else ""
                        logging.info(f"Key '{key}' has value of type {value_type}{size_info}")
                    else:
                        value_preview = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
                        logging.info(f"Key '{key}' has value: {value_preview}")
                
                # Try to find where the item list might be
                potential_items = []
                for key, value in api_response.items():
                    if isinstance(value, list) and len(value) > 0:
                        if isinstance(value[0], dict):
                            logging.info(f"Found potential items list under key '{key}'")
                            logging.info(f"First item keys: {list(value[0].keys())}")
                            potential_items.append((key, value))
                
                # Try to find an offer ID from any potential item lists
                offer_id = None
                for key, items in potential_items:
                    for item in items:
                        if isinstance(item, dict):
                            # Check common ID field names
                            if "OfferId" in item:
                                offer_id = item["OfferId"]
                                logging.info(f"Found OfferId '{offer_id}' in item from '{key}' list")
                                break
                            elif "Id" in item:
                                offer_id = item["Id"]
                                logging.info(f"Found Id '{offer_id}' in item from '{key}' list")
                                break
                    if offer_id:
                        break
            
            elif isinstance(api_response, list):
                logging.info(f"Response is a list with {len(api_response)} items")
                if api_response:
                    sample_item = api_response[0]
                    logging.info(f"Sample item type: {type(sample_item)}")
                    if isinstance(sample_item, dict):
                        logging.info(f"Sample item keys: {list(sample_item.keys())}")
                        # Print a snippet of the sample item
                        logged_sample = {k: v for k, v in sample_item.items() if k in ['OfferId', 'Id', 'Title', 'Url']}
                        logging.info(f"Sample item values: {json.dumps(logged_sample, indent=2)}")
                
                # Try to find an offer ID from the list items
                offer_id = None
                for item in api_response:
                    if isinstance(item, dict):
                        # Check common ID field names
                        if "OfferId" in item:
                            offer_id = item["OfferId"]
                            logging.info(f"Found OfferId: {offer_id}")
                            break
                        elif "Id" in item:
                            offer_id = item["Id"]
                            logging.info(f"Found Id: {offer_id}")
                            break
            
            else:
                logging.info(f"Response is neither a list nor a dict. Type: {type(api_response)}")
                # Dump as string to get a sense of what it is
                logging.info(f"Response preview: {str(api_response)[:500]}...")
                
            # Log full response structure (truncated) for analysis
            response_str = json.dumps(api_response, indent=2)
            logging.info(f"Full response structure (truncated): {response_str[:1000]}...")
                
            # Test the getoffers endpoint if we found an ID
            if offer_id:
                logging.info(f"Testing connection to getoffers endpoint with OfferId: {offer_id}")
                getoffers_headers = {
                    "x-api-key": WOOT_API_KEY,
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
                
                getoffers_response = requests.post(
                    GETOFFERS_ENDPOINT,
                    headers=getoffers_headers,
                    data=json.dumps([offer_id])
                )
                
                if getoffers_response.status_code == 200:
                    detailed_offers = getoffers_response.json()
                    logging.info(f"Successfully connected to getoffers endpoint. Received {len(detailed_offers) if isinstance(detailed_offers, list) else 'non-list'} response.")
                    
                    if isinstance(detailed_offers, list) and detailed_offers:
                        sample_offer = detailed_offers[0]
                        if isinstance(sample_offer, dict):
                            # Log the structure of a detailed offer
                            logging.info(f"Detailed offer keys: {list(sample_offer.keys())}")
                            logged_offer = {k: v for k, v in sample_offer.items() if k in ['Id', 'Title', 'Url']}
                            logging.info(f"Sample detailed offer: {json.dumps(logged_offer, indent=2)}")
                    
                    return True
                else:
                    logging.error(f"Failed to connect to getoffers endpoint. Status code: {getoffers_response.status_code}")
                    logging.error(f"Response: {getoffers_response.text}")
                    # Still return True as the feed endpoint worked
                    return True
            else:
                logging.warning("No suitable ID found in feed items to test getoffers endpoint. Feed endpoint is working though.")
                return True  # Still return True as feed endpoint worked
        else:
            logging.error(f"Failed to connect to feed endpoint. Status code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Error testing Woot API: {e}")
        logging.error(traceback.format_exc())
        return False

def test_email():
    """Test email functionality."""
    logging.info("=== TESTING EMAIL FUNCTIONALITY ===")
    
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not EMAIL_RECIPIENT:
        logging.error("Email configuration is incomplete. Check GMAIL_USER, GMAIL_APP_PASSWORD, and EMAIL_RECIPIENT.")
        return False
    
    try:
        # Create a test email
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Test Email from Woot Deals Service ({datetime.now().isoformat()})"
        msg['From'] = GMAIL_USER
        msg['To'] = EMAIL_RECIPIENT
        
        text_content = "This is a test email to verify the email functionality of the Woot Deals service."
        html_content = f"""
        <html>
        <body>
            <h2>Woot Deals Service - Test Email</h2>
            <p>This is a test email to verify that the email functionality is working correctly.</p>
            <p>Timestamp: {datetime.now().isoformat()}</p>
            <hr>
            <p><small>Sent by your Woot Kindle Deals alert system</small></p>
        </body>
        </html>
        """
        
        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')
        
        msg.attach(part1)
        msg.attach(part2)
        
        # Send the email
        logging.info(f"Attempting to send test email from {GMAIL_USER} to {EMAIL_RECIPIENT}")
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            try:
                logging.info("Connecting to SMTP server...")
                server.ehlo()
                logging.info("SMTP server connected")
                
                logging.info("Attempting login...")
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                logging.info("Login successful")
                
                logging.info("Sending email...")
                server.send_message(msg)
                logging.info("Test email sent successfully")
                return True
            except smtplib.SMTPAuthenticationError as e:
                logging.error(f"SMTP Authentication Error: {e}")
                logging.error("This is likely due to incorrect GMAIL_USER or GMAIL_APP_PASSWORD")
                logging.error("Make sure you're using an App Password, not your regular password")
                logging.error("App Passwords must be generated from your Google Account security settings")
                return False
            except Exception as e:
                logging.error(f"SMTP Error: {e}")
                logging.error(traceback.format_exc())
                return False
    except Exception as e:
        logging.error(f"Error testing email functionality: {e}")
        logging.error(traceback.format_exc())
        return False

def load_seen_deals():
    """
    Load the seen-deal index from Cloud Storage.

    Returns {offer_id: last_seen_iso}, or None if the state could not be read.
    None is deliberately distinct from an empty index: treating a read failure as
    "nothing seen yet" would re-notify about every live deal on Woot. Older
    revisions stored a plain list; that format is accepted and upgraded on save.
    """
    logging.info("Loading seen deals from Cloud Storage")
    try:
        if not storage_client:
            logging.error("Storage client not initialized")
            return None

        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SEEN_DEALS_FILENAME)

        if not blob.exists():
            logging.info(
                f"'{SEEN_DEALS_FILENAME}' does not exist in bucket '{BUCKET_NAME}'. Starting empty."
            )
            return {}

        payload = json.loads(blob.download_as_text())

        if isinstance(payload, list):
            # The legacy format carried no timestamps. Stamp them now so retention
            # has a baseline; anything still live is refreshed by this run anyway.
            now = datetime.now(timezone.utc).isoformat()
            seen_deals = {str(deal_id): now for deal_id in payload if deal_id}
            logging.info(f"Upgraded {len(seen_deals)} seen deals from the legacy list format")
        elif isinstance(payload, dict):
            deals = payload.get("deals", payload)
            if not isinstance(deals, dict):
                logging.error("Seen-deals payload has no usable 'deals' mapping")
                return None
            seen_deals = {str(k): v for k, v in deals.items()}
        else:
            logging.error(f"Unexpected seen-deals payload type: {type(payload).__name__}")
            return None

        logging.info(f"Loaded {len(seen_deals)} seen deals from Cloud Storage")
        return seen_deals
    except Exception as e:
        logging.error(f"Error loading seen deals: {e}")
        logging.error(traceback.format_exc())
        return None


def prune_seen_deals(seen_deals):
    """Drop offers absent from the feed for SEEN_DEALS_RETENTION_DAYS, bounding the state file."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=SEEN_DEALS_RETENTION_DAYS)
    ).isoformat()
    kept = {
        deal_id: last_seen
        for deal_id, last_seen in seen_deals.items()
        if not isinstance(last_seen, str) or last_seen >= cutoff
    }
    dropped = len(seen_deals) - len(kept)
    if dropped:
        logging.info(f"Pruned {dropped} seen deals older than {SEEN_DEALS_RETENTION_DAYS} days")
    return kept

def save_seen_deals(seen_deals):
    """Save the seen-deal index to Cloud Storage."""
    logging.info(f"Attempting to save {len(seen_deals)} seen deals to Cloud Storage")
    try:
        if not storage_client:
            logging.error("Storage client not initialized")
            return False

        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SEEN_DEALS_FILENAME)
        # JSON has no non-string keys, so normalise on the way out to match what
        # load_seen_deals() reads back. Otherwise a non-string ID would miss the
        # seen check on every run and re-alert every hour.
        blob.upload_from_string(
            json.dumps({"version": 2,
                        "deals": {str(k): v for k, v in seen_deals.items()}}),
            content_type="application/json",
        )
        logging.info(f"Saved {len(seen_deals)} seen deals to Cloud Storage")
        return True
    except Exception as e:
        logging.error(f"Error saving seen deals: {e}")
        logging.error(traceback.format_exc())
        return False

def event_pages(kind):
    """Whether an event kind should escalate the run and alert the user."""
    return kind not in NOTABLE_EVENTS


def record_health_event(kind, detail=""):
    """
    Note a problem for this run's health report.

    Anything recorded here lands in the run's summary line. A paging kind also
    escalates the run's status (which Cloud Monitoring alerts on) and, if it is
    severe enough to survive the cooldown, reaches the user as an email and text.
    A kind in NOTABLE_EVENTS is recorded and logged but changes neither.
    """
    _health_events.append({"kind": kind, "detail": str(detail)[:300]})
    line = f"{HEALTH_MARKER}_EVENT kind={kind} detail={detail}"
    if event_pages(kind):
        logging.error(line)
    else:
        logging.warning(line)


def reset_health_events():
    """Clear recorded problems at the start of a run."""
    del _health_events[:]


def load_health_state():
    """Load the cross-run health record. Returns {} when unavailable."""
    try:
        if not storage_client:
            return {}
        blob = storage_client.bucket(BUCKET_NAME).blob(HEALTH_STATE_FILENAME)
        if not blob.exists():
            return {}
        state = json.loads(blob.download_as_text())
        return state if isinstance(state, dict) else {}
    except Exception as e:
        # Never let health bookkeeping break the actual job.
        logging.warning(f"Could not read health state: {e}")
        return {}


def _utc_today():
    """The quota's day. It rolls at 00:00 UTC, not in any local timezone."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_quota_used():
    """
    Requests already spent against today's Woot quota by earlier runs.

    A record from any earlier day is stale -- the quota reset at midnight UTC --
    so it starts over at zero rather than carrying yesterday's total forward.
    """
    # Never raises: quota bookkeeping must not be able to break the job, same
    # rule the rest of the health code follows. Falling back to 0 can at worst
    # permit one extra paginated fallback (~51 requests) against an 800 ceiling,
    # whereas assuming the quota is spent would refuse to run at all.
    try:
        quota = (load_health_state().get("quota") or {})
        if quota.get("date") != _utc_today():
            return 0
        return max(0, int(quota.get("used", 0)))
    except Exception as e:
        logging.warning(f"Could not read quota usage, assuming none spent: {e}")
        return 0


def quota_spent():
    """Requests spent against today's quota, including this run so far."""
    return _quota_prior + _request_count


def quota_remaining():
    """
    Requests left before this run should stop spending.

    Measured against DAILY_REQUEST_CEILING rather than the true 1000, so there is
    always headroom left for the rest of the day.
    """
    return DAILY_REQUEST_CEILING - quota_spent()


def save_health_state(state):
    """Persist the cross-run health record. Best effort."""
    try:
        if not storage_client:
            return False
        blob = storage_client.bucket(BUCKET_NAME).blob(HEALTH_STATE_FILENAME)
        blob.upload_from_string(json.dumps(state), content_type="application/json")
        return True
    except Exception as e:
        logging.warning(f"Could not write health state: {e}")
        return False


def send_alert(subject, body):
    """
    Send a health alert over the same channels as deal alerts.

    Returns False if it could not be sent -- which is precisely the case the
    external Cloud Monitoring alert exists to cover, since a broken mail path
    cannot report that it is broken.
    """
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and EMAIL_RECIPIENT):
        logging.error("Cannot send health alert: mail settings are not configured")
        return False

    try:
        sms = MIMEMultipart('alternative')
        sms['Subject'] = "Woot tracker problem"
        sms['From'] = GMAIL_USER
        sms['To'] = EMAIL_RECIPIENT
        sms.attach(MIMEText(subject[:140], 'plain'))

        email = MIMEMultipart('alternative')
        email['Subject'] = f"Woot tracker: {subject}"
        email['From'] = GMAIL_USER
        email['To'] = GMAIL_USER
        email.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(sms)
            server.send_message(email)

        logging.info(f"Health alert sent: {subject}")
        return True
    except Exception as e:
        logging.error(f"Could not send health alert: {e}")
        logging.error(traceback.format_exc())
        return False


def _recent_alert_count(state, now):
    """How many alerts went out in the last 24 hours."""
    fresh = []
    for stamp in state.get("alert_log", []):
        try:
            if now - datetime.fromisoformat(stamp) < timedelta(hours=24):
                fresh.append(stamp)
        except (TypeError, ValueError):
            continue
    return fresh


def _should_alert(state, status, signature, now):
    """
    Decide whether this problem warrants waking the user.

    Alerts fire on the transition into a problem, then at most once per
    ALERT_COOLDOWN_HOURS while the same problem persists, capped per incident and
    per day. The caps are the backstop against a bug in this logic itself: one
    mistake here must not become unbounded texts at 3am.
    """
    if status == "ok":
        return False

    if len(_recent_alert_count(state, now)) >= MAX_ALERTS_PER_DAY:
        logging.error(
            f"{HEALTH_MARKER}_ALERT_SUPPRESSED reason=daily_cap signature={signature}"
        )
        return False

    if state.get("last_alert_signature") != signature:
        return True  # a different problem always gets through

    if int(state.get("incident_alert_count", 0)) >= MAX_REPEAT_ALERTS_PER_INCIDENT:
        logging.error(
            f"{HEALTH_MARKER}_ALERT_SUPPRESSED reason=incident_cap signature={signature}"
        )
        return False

    last_sent = state.get("last_alert_sent")
    if not last_sent:
        return True

    try:
        elapsed = now - datetime.fromisoformat(last_sent)
    except (TypeError, ValueError):
        return True

    return elapsed >= timedelta(hours=ALERT_COOLDOWN_HOURS)


def report_run_health(status, metrics):
    """
    Emit this run's health summary, alert the user if warranted, and persist the
    cross-run record.

    `status` is "ok", "degraded" (ran, but the results cannot be trusted) or
    "failed" (did not run). Returns the status actually reported.

    Never raises: monitoring must not be able to break the job it monitors.
    """
    try:
        return _report_run_health(status, metrics)
    except Exception as e:
        logging.error(f"Health reporting itself failed: {e}")
        logging.error(traceback.format_exc())
        logging.error(f"{HEALTH_MARKER} status={status} problems=health_reporting_broken")
        return status


def _apply_staleness_check(status, kinds, state, metrics):
    """
    Flag a feed that has stopped changing.

    A single run with no new offers is normal; a full day of them means the feed
    is stale or the seen-index is wrong -- the pipeline looking alive while no
    longer actually observing anything.
    """
    if status != "ok" or "new_items" not in metrics:
        return status, kinds, state

    if int(metrics["new_items"]) != 0:
        state["consecutive_no_new_runs"] = 0
        return status, kinds, state

    stale_runs = int(state.get("consecutive_no_new_runs", 0)) + 1
    state["consecutive_no_new_runs"] = stale_runs
    if stale_runs < NO_NEW_ITEMS_RUNS:
        return status, kinds, state

    record_health_event(
        "feed_not_changing",
        f"no new offers across {stale_runs} consecutive runs; "
        f"the feed may be stale or the seen-index wrong"
    )
    kinds = sorted(set(kinds) - {"none"} | {"feed_not_changing"})
    return "degraded", kinds, state


def _report_run_health(status, metrics):
    now = datetime.now(timezone.utc)
    state = load_health_state()

    events = list(_health_events)
    # Only a paging event may escalate the run. Notable ones ride along in the
    # summary line's notes= field so they stay greppable without alerting, and
    # so status=ok keeps being emitted -- the absence policy watches for exactly
    # that string, and a run that stopped saying it would eventually fire the
    # "has not completed a healthy run" alert instead, which is far worse than
    # the noise being removed.
    paging = [e for e in events if event_pages(e["kind"])]
    notable = [e for e in events if not event_pages(e["kind"])]
    if paging and status == "ok":
        status = "degraded"

    kinds = sorted({e["kind"] for e in paging}) or (["none"] if status == "ok" else ["unknown"])
    notes = sorted({e["kind"] for e in notable})

    status, kinds, state = _apply_staleness_check(status, kinds, state, metrics)

    # Carry the day's request spend forward so the next run knows what is left.
    # Keyed by UTC date because that is when the Woot quota resets; a record from
    # an earlier day is stale and starts over rather than accumulating forever.
    today = _utc_today()
    quota = state.get("quota") or {}
    try:
        prior = int(quota.get("used", 0)) if quota.get("date") == today else 0
    except (TypeError, ValueError):
        prior = 0
    state["quota"] = {"date": today, "used": prior + _request_count}
    # Remember which feeds are capped so the next run alerts only on a change.
    current_caps = capped_feeds(state.get("capped_feeds"))
    if current_caps or state.get("capped_feeds"):
        state["capped_feeds"] = current_caps

    # Both go in the summary line. The daily quota is the limit that actually
    # took this service down, so it belongs in routine output where a trend is
    # visible, not only in the error that fires once it is already gone.
    metrics["requests"] = _request_count
    metrics["quota_used"] = f"{prior + _request_count}/{WOOT_DAILY_QUOTA}"

    # One machine-readable line per run. Cloud Monitoring keys off this: a metric
    # counts status=failed/degraded, and an absence policy fires when no
    # status=ok appears for hours -- the only way to detect the service not
    # running at all, which nothing inside the run can notice.
    detail = " ".join(f"{k}={v}" for k, v in sorted(metrics.items()))
    notes_field = f" notes={','.join(notes)}" if notes else ""
    summary = (
        f"{HEALTH_MARKER} status={status} problems={','.join(kinds)}"
        f"{notes_field} {detail}"
    )
    if status == "ok":
        logging.info(summary)
    else:
        logging.error(summary)

    # Track consecutive failures and the last genuinely good run.
    if status == "ok":
        state["last_success"] = now.isoformat()
        state["consecutive_bad_runs"] = 0
    else:
        state["consecutive_bad_runs"] = int(state.get("consecutive_bad_runs", 0)) + 1

    # Only a fully healthy, complete run may move the baseline. A baseline fed by
    # truncated runs drifts down to meet the failure and disarms the check.
    # `not events` rather than `status == "ok"`: notable events no longer
    # escalate the status, and some of them (feed_shrank, feed_fallback_skipped)
    # describe exactly the partial run that must not be allowed to set the bar.
    if (status == "ok" and not events
            and metrics.get("feed_complete") == "true" and metrics.get("feed_items")):
        sizes = [s for s in state.get("recent_feed_sizes", []) if isinstance(s, int)]
        sizes.append(int(metrics["feed_items"]))
        state["recent_feed_sizes"] = sizes[-FEED_BASELINE_RUNS:]

    state["last_run"] = now.isoformat()
    state["last_status"] = status

    signature = f"{status}:{','.join(kinds)}"
    alerted = False
    if _should_alert(state, status, signature, now):
        lines = [
            f"The Woot deals tracker reported: {status}.",
            "",
            "Problems:",
        ]
        lines += [f"  - {e['kind']}: {e['detail']}" for e in paging] or ["  - (none recorded)"]
        if notable:
            lines += ["", "Also noted (not alerting):"]
            lines += [f"  - {e['kind']}: {e['detail']}" for e in notable]
        lines += [
            "",
            "Run details:",
        ]
        lines += [f"  {k}: {v}" for k, v in sorted(metrics.items())]
        last_success = state.get("last_success")
        lines += [
            "",
            f"Last fully healthy run: {last_success or 'unknown'}",
            f"Consecutive bad runs: {state.get('consecutive_bad_runs')}",
            "",
            f"You will not be alerted about this again for {ALERT_COOLDOWN_HOURS}h "
            f"unless the problem changes.",
        ]
        alerted = send_alert(
            f"{status} ({', '.join(kinds)})",
            "\n".join(lines),
        )
        if alerted:
            if state.get("last_alert_signature") == signature:
                state["incident_alert_count"] = int(state.get("incident_alert_count", 0)) + 1
            else:
                state["incident_alert_count"] = 1
            state["last_alert_signature"] = signature
            state["last_alert_sent"] = now.isoformat()
            state["alert_log"] = (_recent_alert_count(state, now) + [now.isoformat()])[-20:]
        else:
            # Could not tell the user. Make it as loud as possible in the logs so
            # the external monitoring alert is the backstop.
            logging.error(f"{HEALTH_MARKER}_ALERT_FAILED status={status} problems={','.join(kinds)}")

    # Tell the user when things come back, but only if they were told it broke.
    elif status == "ok" and state.get("last_alert_signature"):
        send_alert("recovered", "The Woot deals tracker is working again.")
        state.pop("last_alert_signature", None)
        state.pop("last_alert_sent", None)
        state.pop("incident_alert_count", None)

    save_health_state(state)
    return status


def start_run_budget():
    """Begin the wall-clock and daily-quota budgets for one scheduled run."""
    global _run_deadline, _request_count, _quota_prior
    _run_deadline = time.monotonic() + RUN_BUDGET_SECONDS
    _request_count = 0
    _quota_prior = load_quota_used()
    logging.info(
        f"Run starting with {_quota_prior}/{DAILY_REQUEST_CEILING} of today's "
        f"request ceiling already spent (hard quota {WOOT_DAILY_QUOTA}/day)"
    )


def end_run_budget():
    """Clear the budget so a finished run's deadline cannot leak into the next one."""
    global _run_deadline
    _run_deadline = None


def budget_remaining():
    """Seconds left in this run's budget (infinite when no run is in progress)."""
    if _run_deadline is None:
        return float("inf")
    return _run_deadline - time.monotonic()


def _throttle():
    """Space out Woot API requests so we stay under the API's rate limit."""
    global _last_request_time
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_time)
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


def _retry_after_seconds(response, fallback):
    """Honour a Retry-After header when the API sends one."""
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return max(0.0, min(MAX_RETRY_DELAY, float(header)))
        except (TypeError, ValueError):
            pass
    return fallback


def woot_request(method, url, accept_statuses=(200,), **kwargs):
    """
    Make a rate-limit-aware request to the Woot API.

    Returns the Response once its status is in accept_statuses, or None if the
    request failed or retries were exhausted. A 429 is retried with exponential
    backoff rather than abandoned: giving up on the first 429 is what silently
    truncated the feed to its first ~13 pages.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("x-api-key", WOOT_API_KEY)
    headers.setdefault("Accept", "application/json")

    retry_delay = INITIAL_RETRY_DELAY
    for attempt in range(MAX_RETRIES + 1):
        response = None
        try:
            _throttle()
            # Counted before the call, not after: a request that times out may
            # still have reached the API and spent quota. Over-counting is safe,
            # under-counting is how the daily limit gets blown through again.
            global _request_count
            _request_count += 1
            response = requests.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as e:
            logging.warning(f"Request to {url} failed: {e}")

        if response is not None:
            if response.status_code in accept_statuses:
                return response
            if response.status_code not in (429, 500, 502, 503, 504):
                logging.error(
                    f"Non-retryable {response.status_code} from {url}: {response.text[:200]}"
                )
                return None
            if response.status_code == 429:
                _feed_stats["rate_limit_hits"] = _feed_stats.get("rate_limit_hits", 0) + 1
            logging.warning(f"Retryable {response.status_code} from {url}")

        if attempt == MAX_RETRIES:
            break

        delay = _retry_after_seconds(response, retry_delay) + random.uniform(0, 1)
        if delay > budget_remaining() - 10:
            logging.error(f"Not enough run budget left to retry {url}; giving up")
            return None

        logging.warning(f"Retry {attempt + 1}/{MAX_RETRIES} for {url} in {delay:.1f}s")
        time.sleep(delay)
        retry_delay = min(MAX_RETRY_DELAY, retry_delay * 2)

    logging.error(f"Giving up on {url} after {MAX_RETRIES} retries")
    return None


def _normalise_items(item_list):
    """
    Copy feed items with their ID field normalised.

    The feed calls it OfferId and getoffers calls it Id; carrying both means the
    rest of the pipeline never has to care which endpoint an item came from.
    """
    out = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        processed_item = item.copy()
        if "OfferId" in item:
            processed_item["Id"] = item["OfferId"]
        elif "Id" in item:
            processed_item["OfferId"] = item["Id"]
        out.append(processed_item)
    return out


def _read_feed_payload(response, label):
    """
    Validate one feed response and return its items, or None if unusable.

    Records schema problems rather than raising: a shape change must surface as a
    health event, not as a traceback that looks like a transient outage.
    """
    try:
        api_response = response.json()
    except ValueError as e:
        logging.error(f"Feed {label} was not valid JSON: {e}")
        return None

    if not isinstance(api_response, dict):
        logging.error(f"Unexpected feed payload type: {type(api_response).__name__}")
        return None

    reported_pages = api_response.get("TotalPages")
    if not isinstance(reported_pages, int) or reported_pages <= 0:
        _feed_stats["schema_ok"] = False
    else:
        _feed_stats["reported_pages"] = max(
            _feed_stats.get("reported_pages", 0), reported_pages)

    if "Items" not in api_response:
        _feed_stats["schema_ok"] = False

    item_list = api_response.get("Items")
    if not isinstance(item_list, list):
        logging.error(
            f"Feed {label} has no usable Items list "
            f"(got {type(item_list).__name__})"
        )
        return None, 0
    return item_list, (reported_pages if isinstance(reported_pages, int) else 0)


def _fetch_one_feed_single(feed_name):
    """
    Fetch one named feed in ONE request by omitting the `page` parameter.

    This is the cheap path and the reason the daily quota stopped being a
    constraint: what costs 51 paginated requests costs 1 here.

    Returns (items, ok). There is deliberately NO minimum item count: the feeds
    are legitimately different sizes (Featured had 9 items, Gourmet 0, Home
    5000), so a size floor here would send small feeds into a pointless
    paginated fallback. Truncation is detected by comparing against TotalPages
    instead, and the merged total is size-checked by check_feed_health.
    """
    response = woot_request("GET", f"{FEED_BASE}/{feed_name}")
    if response is None:
        logging.warning(f"Single-request fetch of feed '{feed_name}' failed")
        return [], False

    item_list, reported_pages = _read_feed_payload(response, f"'{feed_name}'")
    if item_list is None:
        return [], False

    items = _normalise_items(item_list)

    # The non-paginated form should return everything at once. Getting back a
    # single page's worth while TotalPages advertises more means the endpoint
    # started paginating on us, and trusting it would silently cost ~98% of
    # this feed's offers.
    if reported_pages > 1 and len(items) <= FEED_PAGE_SIZE:
        logging.warning(
            f"Feed '{feed_name}' returned {len(items)} items but reports "
            f"{reported_pages} pages; treating as paginated, not whole"
        )
        return [], False

    _feed_stats.setdefault("feed_sizes", {})[feed_name] = len(items)
    logging.info(
        f"Feed '{feed_name}': {len(items)} items in one request "
        f"(reports {reported_pages} pages)"
    )
    return items, True


def _fetch_feed_with_fallback(feed_name):
    """
    Fetch one feed, falling back to pagination only if the day can afford it.

    The fallback costs ~51 requests against a 1000/day quota. Spending it on
    every feed of every run would burn 5000+/day and recreate exactly the
    silent overrun this design exists to prevent, so it is gated on the budget.
    """
    items, ok = _fetch_one_feed_single(feed_name)
    if ok:
        return items, True

    if quota_remaining() < PAGINATED_FETCH_COST:
        logging.error(
            f"Skipping paginated fallback for '{feed_name}': only "
            f"{quota_remaining()} requests left under today's ceiling of "
            f"{DAILY_REQUEST_CEILING}, need ~{PAGINATED_FETCH_COST}"
        )
        record_health_event(
            "feed_fallback_skipped",
            f"the single-request fetch of '{feed_name}' failed and the day's "
            f"remaining request budget ({quota_remaining()}) could not afford "
            f"the paginated retry"
        )
        return [], False

    logging.warning(f"Falling back to paginated fetch for feed '{feed_name}'")
    return _fetch_feed_paginated(feed_name)


def capped_feeds(previous=None):
    """
    Feeds at or near Woot's 5000-item ceiling, and so hiding inventory.

    Reported every run for visibility; only a CHANGE in this set is worth
    alerting on, since the already-capped feeds would otherwise alert forever.

    Entry and exit deliberately use different thresholds. With one threshold a
    feed parked at the warn line crosses it back and forth on ordinary churn and
    re-reports itself as newly capped each time -- Home did exactly that four
    times in ten days. A feed already in `previous` therefore stays in the set
    until it drops below the lower clear line, so only a real change moves it.
    """
    sizes = _feed_stats.get("feed_sizes") or {}
    enter = WOOT_FEED_ITEM_CAP * FEED_CAP_WARN_RATIO
    clear = WOOT_FEED_ITEM_CAP * FEED_CAP_CLEAR_RATIO
    prev = set(previous or [])
    return sorted(name for name, count in sizes.items()
                  if count >= enter or (name in prev and count >= clear))


def fetch_feed():
    """
    Fetch every Woot feed and merge them into one deduplicated catalogue.

    All alone is capped at 5000 items and hides roughly 60% of the catalogue,
    including the Clearance and Sellout offers where discounted e-readers turn
    up. The category feeds together are a superset, so they are all polled and
    merged by offer id.

    Returns (items, complete). `complete` is False when ANY feed could not be
    read, so the caller does not record offers it never looked at as "seen" --
    a partial read must never let unseen offers be marked seen.
    """
    logging.info(f"Fetching {len(FEED_NAMES)} Woot feeds")

    _feed_stats.clear()
    _feed_stats.update({"pages_fetched": 0, "reported_pages": 0,
                        "rate_limit_hits": 0, "schema_ok": True,
                        "feeds_ok": 0, "feeds_failed": 0, "raw_items": 0})

    merged = {}
    failed = []

    for feed_name in FEED_NAMES:
        # Leave room for the detail fetch rather than spending the whole run on
        # feeds; the remaining ones are picked up next run.
        if budget_remaining() < FEED_BUDGET_RESERVE:
            logging.error(
                f"Run budget exhausted before feed '{feed_name}'; "
                f"{len(FEED_NAMES) - len(failed) - _feed_stats['feeds_ok']} feeds unread"
            )
            failed.append(feed_name)
            continue

        items, ok = _fetch_feed_with_fallback(feed_name)

        # Keep whatever a partial read did return. A feed that was cut short
        # still yields real offers, and dropping them would hide deals for no
        # gain -- `complete` below is what stops unseen offers being recorded
        # as seen, so partial data is safe to use but not safe to trust as whole.
        _feed_stats["raw_items"] += len(items)
        for item in items:
            offer_id = item.get("OfferId") or item.get("Id")
            if offer_id:
                merged[offer_id] = item

        if ok:
            _feed_stats["feeds_ok"] += 1
        else:
            failed.append(feed_name)
            _feed_stats["feeds_failed"] += 1

    all_items = list(merged.values())
    complete = not failed
    if failed:
        logging.error(
            f"Feeds that could not be read: {', '.join(failed)}; "
            f"marking this run incomplete so nothing unseen is recorded as seen"
        )

    logging.info(
        f"Merged {_feed_stats['raw_items']} items from "
        f"{_feed_stats['feeds_ok']}/{len(FEED_NAMES)} feeds into "
        f"{len(all_items)} distinct offers, complete={complete}"
    )
    return all_items, complete


def _fetch_feed_paginated(feed_name="All"):
    """
    Fetch one feed a page at a time -- the original, expensive path.

    Kept as a fallback so a change in the single-request endpoint degrades to
    something proven rather than to nothing.
    """
    all_items = []
    current_page = 1
    total_pages = 1
    complete = True

    while current_page <= total_pages and current_page <= MAX_FEED_PAGES:
        if budget_remaining() < FEED_BUDGET_RESERVE:
            logging.error(
                f"Run budget exhausted after {current_page - 1}/{total_pages} feed pages; "
                f"remaining pages will be picked up on the next run"
            )
            complete = False
            break

        page_url = f"{FEED_BASE}/{feed_name}?page={current_page}"
        response = woot_request("GET", page_url, accept_statuses=(200, 404))
        if response is None:
            logging.error(f"Failed to fetch feed page {current_page}/{total_pages}")
            complete = False
            break

        if response.status_code == 404:
            # The API reports one more page than it actually serves, so a 404 on
            # the last advertised page is the real end of the feed. A 404 before
            # that is a route/permission change and must not read as a clean end.
            if all_items and current_page >= total_pages:
                logging.info(
                    f"Feed ended at page {current_page - 1} "
                    f"(the API reported {total_pages} pages)"
                )
            else:
                logging.error(
                    f"Feed page {current_page} of {total_pages} returned 404 "
                    f"after reading {len(all_items)} items"
                )
                complete = False
            break

        # A malformed page is an API shape change, not the end of the feed, so it
        # must never masquerade as a complete read.
        item_list, reported_pages = _read_feed_payload(
            response, f"'{feed_name}' page {current_page}")
        if item_list is None:
            complete = False
            break

        # Use THIS feed's page count, not _feed_stats["reported_pages"], which is
        # the maximum across every feed -- paginating Shirts (3 pages) against
        # All's 51 would chase 48 pages that do not exist.
        # Without a usable TotalPages the loop would read page 1, find total_pages
        # still at its initial 1, and exit reporting a complete feed of 100 items.
        # _read_feed_payload already flagged that as a schema problem.
        if reported_pages:
            if reported_pages > MAX_FEED_PAGES:
                logging.warning(
                    f"Feed reports {reported_pages} pages, capping at {MAX_FEED_PAGES}"
                )
                complete = False
            total_pages = min(reported_pages, MAX_FEED_PAGES)

        if not item_list:
            # An empty page at the advertised end is a normal finish; an empty
            # page in the middle means the feed came back short.
            if current_page >= total_pages:
                logging.info(f"Feed page {current_page} was empty; end of feed")
            else:
                logging.error(
                    f"Feed page {current_page} of {total_pages} was empty; feed came back short"
                )
                complete = False
            break

        all_items.extend(_normalise_items(item_list))

        logging.info(
            f"Feed page {current_page}/{total_pages}: {len(item_list)} items "
            f"({len(all_items)} total so far)"
        )
        _feed_stats["pages_fetched"] += 1
        current_page += 1

    logging.info(
        f"Fetched {len(all_items)} feed items across {current_page - 1} page(s), complete={complete}"
    )
    return all_items, complete

def fetch_detailed_offers(offer_ids):
    """
    Fetch full offer details for the given IDs, in batches, through the shared
    rate limiter.

    Returns (offers, fetched_ids). fetched_ids lists only the IDs we actually got
    a response for, so the caller can leave the rest unseen and retry them next run.
    """
    if not offer_ids:
        logging.info("No offer IDs provided. Skipping detailed offers fetch.")
        return [], []

    all_detailed_offers = []
    fetched_ids = []
    total_batches = (len(offer_ids) + DETAIL_BATCH_SIZE - 1) // DETAIL_BATCH_SIZE

    for i in range(0, len(offer_ids), DETAIL_BATCH_SIZE):
        batch = offer_ids[i:i + DETAIL_BATCH_SIZE]
        batch_num = i // DETAIL_BATCH_SIZE + 1

        if budget_remaining() < 15:
            logging.error(
                f"Run budget exhausted after {batch_num - 1}/{total_batches} detail batches; "
                f"{len(offer_ids) - len(fetched_ids)} offers deferred to the next run"
            )
            break

        logging.info(
            f"Fetching detail batch {batch_num}/{total_batches} with {len(batch)} offer IDs"
        )
        response = woot_request(
            "POST",
            GETOFFERS_ENDPOINT,
            data=json.dumps(batch),
            headers={"Content-Type": "application/json"},
        )
        if response is None:
            logging.error(f"Detail batch {batch_num}/{total_batches} failed; deferring to next run")
            continue

        try:
            detailed_offers = response.json()
        except ValueError as e:
            logging.error(f"Detail batch {batch_num} was not valid JSON: {e}")
            continue

        if not isinstance(detailed_offers, list):
            logging.warning(
                f"Detailed offers response is not a list: {type(detailed_offers).__name__}"
            )
            continue

        all_detailed_offers.extend(detailed_offers)

        # Record only the IDs actually present in the response. Crediting the
        # whole batch would mark silently-omitted offers as seen without ever
        # keyword-checking them, and they are exactly the ones that passed the
        # pre-filter.
        returned = {o.get("Id") or o.get("OfferId") for o in detailed_offers
                    if isinstance(o, dict)}
        present = [offer_id for offer_id in batch if offer_id in returned]
        fetched_ids.extend(present)
        if len(present) != len(batch):
            logging.warning(
                f"Detail batch {batch_num}: asked for {len(batch)} offers, "
                f"{len(present)} came back; the rest will be retried"
            )

    logging.info(
        f"Total detailed offers fetched: {len(all_detailed_offers)} "
        f"for {len(fetched_ids)}/{len(offer_ids)} requested IDs"
    )
    return all_detailed_offers, fetched_ids

def is_matching_deal(deal):
    """Check whether a detailed offer matches our keywords."""
    deal_id = deal.get("Id", deal.get("OfferId", "unknown"))

    for field in ("Title", "Subtitle", "WriteUpBody", "Features", "Snippet", "Slug"):
        hits = matched_keywords(deal.get(field))
        if hits:
            logging.info(f"Deal {deal_id} matches {hits} in field '{field}'")
            return True

    return False

def filter_deals(deals, seen_deals):
    """Return the deals that match our keywords and have not been seen before."""
    logging.info(f"Filtering {len(deals)} detailed offers against {len(seen_deals)} seen deals")
    new_matching_deals = []

    for deal in deals:
        unique_id = deal.get("Id", deal.get("OfferId"))
        if not unique_id:
            logging.warning(f"Detailed offer has no Id or OfferId: {deal.get('Title', '?')}")
            continue

        if unique_id in seen_deals:
            continue

        if is_matching_deal(deal):
            new_matching_deals.append(deal)

    logging.info(f"Found {len(new_matching_deals)} new matching deals.")
    return new_matching_deals

def format_deal_notifications(deal):
    """Format a deal for both email and text notifications."""
    deal_id = deal.get("Id", deal.get("OfferId", "unknown"))
    logging.info(f"Formatting notifications for deal {deal_id}")
    
    # These fields are sometimes present but null in the live feed, so a plain
    # .get() default is not enough -- it only fires when the key is absent.
    title = deal.get("Title") or "No Title"
    url = deal.get("Url") or "No URL"
    
    # Get price information - handle different possible structures
    sale_price = None
    list_price = None
    price_info = "Price unknown"
    
    # Try to get price from Items field
    items = deal.get("Items", [])
    if isinstance(items, list) and items and isinstance(items[0], dict):
        sale_price = items[0].get("SalePrice", None)
        list_price = items[0].get("ListPrice", None)
        
        if sale_price is not None:
            price_info = f"${sale_price}"
    
    # Try SalePrice field directly on the deal
    elif "SalePrice" in deal:
        sale_price_data = deal.get("SalePrice")
        list_price = deal.get("ListPrice")
        
        if (isinstance(sale_price_data, list) and sale_price_data
                and isinstance(sale_price_data[0], dict)):
            # Handle price range format
            min_price = sale_price_data[0].get("Minimum", None)
            if min_price is not None:
                sale_price = min_price
                price_info = f"${min_price}"
        elif sale_price_data is not None:
            sale_price = sale_price_data
            price_info = f"${sale_price}"
    
    # Format savings info if we have both prices
    savings_info = ""
    if sale_price is not None and list_price is not None:
        if isinstance(sale_price, (int, float)) and isinstance(list_price, (int, float)):
            if list_price > sale_price:
                savings_info = f" (Save ${list_price - sale_price:.2f})"
    
    # 1. Create short text message (<140 chars)
    list_price_text = f" (Was ${list_price})" if list_price is not None else ""
    text_message = f"{title[:70]}... {price_info}{list_price_text}"
    
    # Ensure we're under 140 chars
    if len(text_message) > 140:
        # Truncate title further if needed
        max_title_len = 70 - (len(text_message) - 140)
        if max_title_len < 10:
            max_title_len = 10
        text_message = f"{title[:max_title_len]}... {price_info}{list_price_text}"
    
    # 2. Format the detailed email
    email_body = f"""
<h2>{title}</h2>
<p><strong>Price:</strong> {price_info}{savings_info}</p>
<p><strong>URL:</strong> <a href="{url}">{url}</a></p>
<hr>
<p><small>Sent by your Woot Deals alert system</small></p>
"""
    
    logging.info(f"Notifications formatted for deal {deal_id}")
    return title, email_body, text_message

def send_notifications(deals):
    """Send email and text notifications for new deals. Returns True on success."""
    if not deals:
        logging.info("No deals to send notifications for. Skipping.")
        return False

    logging.info(f"Preparing to send notifications for {len(deals)} deals")
    try:
        # Send text messages to the phone number
        text_msg = MIMEMultipart('alternative')
        text_msg['Subject'] = f"Woot Deal Alert"
        text_msg['From'] = GMAIL_USER
        text_msg['To'] = EMAIL_RECIPIENT  # This is the phone number email

        # Send detailed emails to the sender's address
        email_msg = MIMEMultipart('alternative')
        email_msg['Subject'] = f"Woot Alert: {len(deals)} new deal(s) matching your keywords"
        email_msg['From'] = GMAIL_USER
        email_msg['To'] = GMAIL_USER  # Send to yourself

        text_parts = []
        html_parts = []

        formatted = 0
        for deal in deals:
            # Isolate per-deal formatting: a single malformed offer used to raise
            # here, fail the whole send, and defer every good deal with it -- then
            # do the same thing again on every subsequent run, forever.
            try:
                title, html_content, _sms_content = format_deal_notifications(deal)
            except Exception as e:
                deal_id = deal.get("Id", deal.get("OfferId", "unknown"))
                logging.error(f"Could not format deal {deal_id}, skipping it: {e}")
                record_health_event("deal_format_failed", f"offer {deal_id}: {e}")
                continue
            text_parts.append(f"{title} - {deal.get('Url') or 'No URL'}")
            html_parts.append(html_content)
            formatted += 1

        if not formatted:
            logging.error("No deals could be formatted; nothing to send")
            return False

        # For SMS - use a simple summary format instead of listing each deal.
        # Go through matched_keywords() so a field that is present but null does
        # not raise and take the whole notification down with it.
        keyword_hits = set()
        for deal in deals:
            for field in ("Title", "Subtitle", "WriteUpBody", "Features", "Snippet"):
                keyword_hits.update(matched_keywords(deal.get(field)))

        # Create a comma-separated list of matched keywords
        keywords_str = ", ".join(sorted(keyword_hits))
        # Truncate if too long
        if len(keywords_str) > 50:
            keywords_str = keywords_str[:47] + "..."

        # Create the summary message
        sms_content = f"({len(deals)}) deals found for keywords: {keywords_str}"
        logging.info(f"SMS summary: {sms_content}")
        text_msg.attach(MIMEText(sms_content, 'plain'))

        # For email - use full HTML
        text_content = "\n\n".join(text_parts)
        html_content = "<html><body>" + "".join(html_parts) + "</body></html>"
        email_msg.attach(MIMEText(text_content, 'plain'))
        email_msg.attach(MIMEText(html_content, 'html'))

        # Send both messages
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)

            # Send text message first
            server.send_message(text_msg)
            logging.info("Text message sent successfully")

            # Send detailed email
            server.send_message(email_msg)
            logging.info("Email notification sent successfully")

        logging.info(f"Sent notifications for {len(deals)} deals")
        return True

    except Exception as e:
        logging.error(f"Error sending notifications: {e}")
        logging.error(traceback.format_exc())
        return False

def run_all_tests():
    """Run all diagnostic tests."""
    logging.info("====== RUNNING ALL DIAGNOSTIC TESTS ======")
    
    results = {
        "environment_variables": test_environment_variables(),
        "storage_access": test_storage_access(),
        "woot_api": test_woot_api(),
        "email": test_email(),
        "api_structure": test_woot_api_structure()  # Add our new test
    }
    
    logging.info("====== TEST RESULTS SUMMARY ======")
    all_passed = True
    for test_name, result in results.items():
        logging.info(f"{test_name}: {'PASS' if result else 'FAIL'}")
        if not result:
            all_passed = False
    
    logging.info(f"Overall test result: {'PASS' if all_passed else 'FAIL'}")
    return results

def improved_title_contains_keywords(item):
    """
    Check whether a feed item mentions any keyword.

    Used to pre-filter the feed so getoffers calls (which are the rate-limited,
    expensive ones) are only spent on plausible matches.
    """
    if not isinstance(item, dict):
        return False

    fields_to_check = [
        "Title", "title",
        "Description", "description",
        "Subtitle", "subtitle",
        "Snippet", "snippet",
        "Summary", "summary",
        "Name", "name",
        "ProductName", "productName",
        "WriteUpBody", "writeUpBody",
        "Features", "features",
        # Slug carries the product name even when Title is generic; normalize_text
        # flattens its hyphens so multi-word keywords still match.
        "Slug", "slug",
    ]

    for field in fields_to_check:
        hits = matched_keywords(item.get(field))
        if hits:
            item_id = item.get("OfferId", item.get("Id", "unknown"))
            logging.info(f"Pre-filter hit on {item_id}: {hits} in field '{field}'")
            return True

    categories = item.get("Categories")
    if isinstance(categories, list):
        hits = matched_keywords(" ".join(c for c in categories if isinstance(c, str)))
        if hits:
            item_id = item.get("OfferId", item.get("Id", "unknown"))
            logging.info(f"Pre-filter hit on {item_id}: {hits} in Categories")
            return True

    return False

def check_woot_deals(request):
    """
    Main function to check for Woot deals.

    Returns (body, http_status). A hard failure returns 5xx so Cloud Scheduler
    records the job as failed instead of quietly going green, which is what let
    the original bug hide for months.
    """
    # Extract test mode from request if provided
    test_mode = None
    if request and hasattr(request, 'args') and request.args:
        test_mode = request.args.get('test')

    logging.info(f"====== STARTING WOOT DEALS CHECK {'(TEST MODE: ' + test_mode + ')' if test_mode else ''} ======")

    # If a specific test is requested, run only that test
    if test_mode:
        start_run_budget()  # diagnostics make API calls too, so pace them
        try:
            if test_mode == "env":
                test_environment_variables()
                return "Environment variables test completed. Check logs for results.", 200
            elif test_mode == "storage":
                test_storage_access()
                return "Storage access test completed. Check logs for results.", 200
            elif test_mode == "api":
                test_woot_api()
                return "Woot API test completed. Check logs for results.", 200
            elif test_mode == "email":
                test_email()
                return "Email test completed. Check logs for results.", 200
            elif test_mode == "structure":
                test_woot_api_structure()
                return "Woot API structure test completed. Check logs for results.", 200
            elif test_mode == "all":
                run_all_tests()
                return "All diagnostic tests completed. Check logs for results.", 200
        finally:
            end_run_budget()

    # Regular operation
    logging.info("Starting regular operation")
    reset_health_events()
    start_run_budget()
    global _feed_was_fetched
    _feed_was_fetched = False
    try:
        return _run_deal_check()
    finally:
        end_run_budget()


def _run_deal_check():
    """One full pass over the feed. Returns (body, http_status)."""
    metrics = {"feed_items": 0, "feed_complete": "false", "matches": 0, "notified": "false"}

    if not storage_client:
        # Set at import time; if it failed, this instance can never read or write
        # state and will abort every run until it is replaced.
        record_health_event("storage_client_uninitialized",
                            "Cloud Storage client failed to initialize at startup")
        report_run_health("failed", metrics)
        return "Error: storage client unavailable", 503

    if not test_environment_variables():
        record_health_event("missing_env_vars", "one or more required settings are unset")
        report_run_health("failed", metrics)
        return "Error: Missing required environment variables", 500

    # Load previously seen deal IDs. A read failure is fatal for this run: carrying
    # on with an empty index would treat every live deal as new and spam alerts.
    seen_deals = load_seen_deals()
    if seen_deals is None:
        record_health_event("seen_state_unreadable",
                            f"could not read {SEEN_DEALS_FILENAME} from {BUCKET_NAME}")
        report_run_health("failed", metrics)
        return "Error: could not read seen-deals state", 503

    # Step 1: fetch the feed
    global _feed_was_fetched
    feed_items, feed_complete = fetch_feed()
    _feed_was_fetched = True
    metrics["feed_items"] = len(feed_items)
    metrics["feed_complete"] = str(feed_complete).lower()

    if not feed_items:
        record_health_event("feed_empty", "the feed returned no items at all")
        report_run_health("failed", metrics)
        return "Error: no feed items found", 502

    if not feed_complete:
        # Deliberately not a 5xx: an immediate scheduler retry would just spend
        # more of the rate-limit budget. The health report is the alert path.
        record_health_event("feed_incomplete",
                            f"only {len(feed_items)} items read before the feed was cut short")

    # Step 2: pre-filter on the feed's own text fields so getoffers calls are only
    # spent on plausible matches.
    feed_ids = []
    potential_matches = []
    already_seen = 0

    for item in feed_items:
        offer_id = item.get("OfferId") or item.get("Id")
        if not offer_id:
            continue

        feed_ids.append(offer_id)

        if offer_id in seen_deals:
            already_seen += 1
            continue

        if improved_title_contains_keywords(item):
            potential_matches.append(offer_id)

    check_feed_health(len(feed_items), feed_ids, feed_items, feed_complete)

    metrics["feeds"] = f"{_feed_stats.get('feeds_ok', 0)}/{len(FEED_NAMES)}"
    metrics["capped"] = ",".join(_feed_stats.get("capped_now") or capped_feeds()) or "none"
    metrics["raw_items"] = _feed_stats.get("raw_items", 0)
    metrics["pages"] = _feed_stats.get("pages_fetched", 0)
    metrics["rate_limit_hits"] = _feed_stats.get("rate_limit_hits", 0)
    metrics["canary_hits"] = count_canary_hits(feed_items)
    metrics["new_items"] = len(feed_ids) - already_seen
    metrics["potential_matches"] = len(potential_matches)
    logging.info(
        f"Pre-filtered {len(feed_items)} feed items: {already_seen} already seen, "
        f"{metrics['new_items']} new, {len(potential_matches)} potential matches"
    )

    # Step 3: pull full details for the potential matches and confirm them
    matching_deals = []
    processed_ids = []
    if potential_matches:
        detailed_offers, processed_ids = fetch_detailed_offers(potential_matches)
        matching_deals = filter_deals(detailed_offers, seen_deals)

        missed = len(potential_matches) - len(processed_ids)
        if missed:
            record_health_event(
                "detail_fetch_incomplete",
                f"{missed} of {len(potential_matches)} candidate offers could not be checked"
            )

    metrics["matches"] = len(matching_deals)

    # Step 4: notify
    notified = False
    if matching_deals:
        logging.info(f"Found {len(matching_deals)} new matching deals. Sending notifications.")
        notified = send_notifications(matching_deals)
        if not notified:
            record_health_event(
                "notification_failed",
                f"{len(matching_deals)} matching deals could not be sent; they stay queued"
            )
    metrics["notified"] = str(notified).lower()

    # Step 5: record what we actually evaluated. Offers whose details we could not
    # fetch, and matches we could not notify about, are deliberately left out so
    # the next run picks them up again.
    deferred = set(potential_matches) - set(processed_ids)
    if not notified:
        for deal in matching_deals:
            unique_id = deal.get("Id", deal.get("OfferId"))
            if unique_id:
                deferred.add(unique_id)

    now_iso = datetime.now(timezone.utc).isoformat()
    for offer_id in feed_ids:
        if offer_id not in deferred:
            seen_deals[offer_id] = now_iso

    # Only prune after a complete feed: a truncated run has not refreshed the
    # timestamps of offers living on the pages it never reached.
    if feed_complete:
        seen_deals = prune_seen_deals(seen_deals)

    if not save_seen_deals(seen_deals):
        # Left unchecked this is the worst storm vector in the service: state
        # never advances, so every matching deal is re-sent every single hour.
        record_health_event(
            "seen_state_unwritable",
            "could not save seen deals; matching offers would be re-sent every run"
        )
    metrics["seen_deals"] = len(seen_deals)
    metrics["deferred"] = len(deferred)

    # A state file that reads and writes cleanly can still hold the wrong thing.
    # Truncation is the dangerous direction and keeps paging: an index that lost
    # its contents re-notifies every live deal on Woot. Oversize is not urgent --
    # the file still works, it is just bigger than expected -- so it only notes.
    if len(seen_deals) < SEEN_STATE_MIN:
        record_health_event(
            "seen_state_implausible",
            f"the seen index holds only {len(seen_deals)} entries, under the "
            f"{SEEN_STATE_MIN} floor; matching deals would be re-sent"
        )
    elif len(seen_deals) > SEEN_STATE_MAX:
        record_health_event(
            "seen_state_oversized",
            f"the seen index holds {len(seen_deals)} entries, over the "
            f"{SEEN_STATE_MAX} runaway backstop"
        )

    # Ask the question the old size ceiling was really standing in for. After a
    # complete run the prune has just executed, so nothing may still be older
    # than the retention window; anything that is means retention has stopped
    # working and the file will grow without bound. Stating the invariant
    # directly means it cannot go stale when the catalogue changes size, which
    # is exactly how the 90000 ceiling turned into a day of false alarms.
    if feed_complete and seen_deals:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=SEEN_DEALS_RETENTION_DAYS)
        ).isoformat()
        unpruned = sum(1 for v in seen_deals.values()
                       if isinstance(v, str) and v < cutoff)
        if unpruned:
            record_health_event(
                "seen_state_unpruned",
                f"{unpruned} entries are older than the "
                f"{SEEN_DEALS_RETENTION_DAYS}-day retention but survived the prune"
            )

    metrics["duration_s"] = round(RUN_BUDGET_SECONDS - budget_remaining(), 1)
    if metrics["duration_s"] > RUN_BUDGET_SECONDS * RUN_DURATION_WARN_RATIO:
        record_health_event(
            "run_near_deadline",
            f"the run took {metrics['duration_s']}s of a {RUN_BUDGET_SECONDS}s budget"
        )

    status = report_run_health("ok", metrics)

    result_message = (
        f"Feed items: {len(feed_items)} (complete={feed_complete}); "
        f"potential matches: {len(potential_matches)}; "
        f"new matching deals: {len(matching_deals)}; "
        f"notified: {notified}; deferred: {len(deferred)}; health: {status}"
    )
    logging.info(result_message)
    # A degraded run still did useful work, so it is not a scheduler failure; the
    # health report is what raises it.
    return result_message, 200


def feed_baseline(state):
    """
    The median feed size over recent healthy runs, or None without enough data.

    A median rather than a mean so one anomalous run cannot move it, and only
    healthy runs contribute -- a baseline fed by truncated runs walks down to
    meet the failure and quietly disarms the check.
    """
    sizes = [s for s in state.get("recent_feed_sizes", []) if isinstance(s, int)]
    if len(sizes) < FEED_BASELINE_MIN_SAMPLES:
        return None
    ordered = sorted(sizes)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def check_feed_health(feed_items, feed_ids, items, feed_complete):
    """
    Check the feed's shape and content, not just that a request succeeded.

    Every check here exists because the original failure kept every ordinary
    signal green -- HTTP 200, no exception, a plausible-looking run -- while the
    data underneath was wrong.
    """
    # Contract: the feed must tell us how many pages there are and must carry an
    # Items key. Without that the pagination loop silently reads one page.
    if not _feed_stats.get("schema_ok", True):
        record_health_event(
            "feed_schema_invalid",
            "the feed response is missing TotalPages or Items, or they changed type"
        )

    # Did we actually read the whole feed? Page count is a far finer measure of
    # truncation than item count. The -1 allows for the known off-by-one where
    # the API advertises one more page than it serves.
    # Coverage is now counted in feeds, not pages: one request returns a whole
    # feed, so page count says nothing about completeness. Truncation of an
    # individual feed is caught at fetch time by comparing its item count against
    # its own TotalPages, which marks that feed failed and the run incomplete.
    feeds_ok = _feed_stats.get("feeds_ok", 0)
    feeds_failed = _feed_stats.get("feeds_failed", 0)
    if feeds_ok and feeds_ok < len(FEED_NAMES):
        record_health_event(
            "feed_coverage_partial",
            f"read {feeds_ok} of {len(FEED_NAMES)} feeds "
            f"({feeds_failed} failed); the catalogue seen this run is incomplete"
        )

    if feed_items < FEED_SIZE_FLOOR:
        record_health_event(
            "feed_too_small",
            f"only {feed_items} items, expected at least {FEED_SIZE_FLOOR}"
        )

    # There is deliberately no duplicate-id check any more. It existed to catch
    # pagination silently returning the same page, which inflated the item count
    # past every size check. Feeds are now merged into a dict keyed by offer id,
    # so that inflation cannot happen: repeated pages collapse to the distinct
    # offers they contain and the collapse trips FEED_SIZE_FLOOR instead. Keeping
    # the check would only suggest a protection that can no longer fire.

    # If the feed stops carrying title text, the matcher matches nothing while
    # every count, page and status stays green -- the original bug's twin.
    if items:
        with_text = sum(1 for i in items if isinstance(i.get("Title"), str) and i["Title"].strip())
        ratio = with_text / len(items)
        if ratio < FEED_MIN_TITLE_RATIO:
            record_health_event(
                "feed_text_missing",
                f"only {ratio:.0%} of feed items carry a title; the matcher cannot see them"
            )

    if _feed_stats.get("rate_limit_hits"):
        logging.info(
            f"Feed fetch absorbed {_feed_stats['rate_limit_hits']} rate-limit responses"
        )

    # Still worth watching even though a healthy run no longer paginates: this is
    # what the paginated FALLBACK would have to get through, so once it outgrows
    # the run budget the fallback has quietly stopped being a real safety net.
    reported = _feed_stats.get("reported_pages", 0)
    if reported > FEED_PAGES_WARN:
        record_health_event(
            "feed_outgrowing_budget",
            f"the largest feed now reports {reported} pages; the run budget "
            f"supports about "
            f"{int((RUN_BUDGET_SECONDS - FEED_BUDGET_RESERVE) / MIN_REQUEST_INTERVAL)}"
        )

    # A capped feed is silent by nature: the run still reports every feed read and
    # complete. Alert only when a feed NEWLY reaches the ceiling -- the ones
    # already there would otherwise fire on every run forever.
    try:
        _state_for_checks = load_health_state()
    except Exception:
        _state_for_checks = {}
    _prev_caps = _state_for_checks.get("capped_feeds") or []
    # Stash the deadbanded set so the summary line reports the same thing that
    # gets stored and compared, rather than the raw at-or-above-entry set.
    _feed_stats["capped_now"] = capped_feeds(_prev_caps)
    newly_capped = [f for f in _feed_stats["capped_now"] if f not in set(_prev_caps)]
    if newly_capped:
        record_health_event(
            "feed_newly_capped",
            f"{', '.join(newly_capped)} reached Woot's {WOOT_FEED_ITEM_CAP}-item "
            f"ceiling; offers past it cannot be fetched by any request, so this "
            f"feed's coverage is now partial and will stay that way"
        )

    try:
        baseline = feed_baseline(load_health_state())
    except Exception as e:
        logging.warning(f"Could not read the feed baseline: {e}")
        return

    if baseline and feed_items < baseline * FEED_SHRINK_RATIO:
        record_health_event(
            "feed_shrank",
            f"{feed_items} items against a recent median of {baseline}"
        )


def count_canary_hits(items):
    """
    Count feed items mentioning a term Woot always carries.

    Observe-only for now: a matcher regression would drive this to zero within an
    hour, but the threshold should not be armed until a week of data confirms the
    term really does appear in every run.
    """
    if not CANARY_KEYWORD:
        return 0
    needle = normalize_text(CANARY_KEYWORD)
    hits = 0
    for item in items:
        for field in ("Title", "Subtitle", "Slug"):
            value = item.get(field)
            if isinstance(value, str) and needle in normalize_text(value):
                hits += 1
                break
    return hits

def test_woot_api_structure():
    """Test the Woot API response structure and prefiltering logic."""
    logging.info("=== TESTING WOOT API STRUCTURE AND PREFILTERING ===")
    
    if not WOOT_API_KEY:
        logging.error("WOOT_API_KEY is not set")
        return False
    
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json"
    }
    
    try:
        # Test the feed endpoint
        logging.info(f"Testing connection to feed endpoint: {FEED_ENDPOINT}")
        response = requests.get(FEED_ENDPOINT, headers=headers)
        
        if response.status_code == 200:
            api_response = response.json()
            logging.info(f"Successfully connected to feed endpoint. Received response data.")
            
            # Log the response structure type
            response_type = type(api_response).__name__
            logging.info(f"API response is a {response_type}")
            
            # Analyze the response structure
            if isinstance(api_response, dict):
                logging.info(f"API response is a DICTIONARY with keys: {list(api_response.keys())}")
                items_field = None
                
                # Find the items field
                if "Items" in api_response and isinstance(api_response["Items"], list):
                    items_field = "Items"
                    items_data = api_response["Items"]
                    
                    logging.info(f"Found items list in field '{items_field}' with {len(items_data)} items")
                    
                    # Sample available fields in first item
                    if items_data and len(items_data) > 0:
                        sample_item = items_data[0]
                        logging.info(f"Sample item fields: {list(sample_item.keys())}")
                        
                        # Check if our keywords are present in any items at all (expanded search)
                        total_matches = 0
                        checked_items = min(50, len(items_data))  # Check more items
                        
                        for i, item in enumerate(items_data[:checked_items]):
                            # Test our improved matching function
                            if improved_title_contains_keywords(item):
                                total_matches += 1
                                logging.info(f"Item {i} matches using improved prefiltering")
                                
                                # Log which keywords were found and in which fields
                                for field in item.keys():
                                    if isinstance(item[field], str):
                                        for keyword in KEYWORDS:
                                            if keyword.lower() in item[field].lower():
                                                preview = item[field][:50] + "..." if len(item[field]) > 50 else item[field]
                                                logging.info(f"Keyword '{keyword}' found in field '{field}': {preview}")
                            
                        logging.info(f"Found {total_matches} matching items out of {checked_items} checked items using improved_title_contains_keywords()")
                    
                else:
                    logging.warning("Could not find Items list in the response")
                    
            elif isinstance(api_response, list):
                logging.info(f"API response is a LIST with {len(api_response)} items")
                
                # Sample available fields in first item
                if api_response and len(api_response) > 0:
                    sample_item = api_response[0]
                    logging.info(f"Sample item fields: {list(sample_item.keys())}")
                    
                    # Check if our keywords are present in any items at all (expanded search)
                    total_matches = 0
                    checked_items = min(50, len(api_response))  # Check more items
                    
                    for i, item in enumerate(api_response[:checked_items]):
                        # Test our improved matching function 
                        if improved_title_contains_keywords(item):
                            total_matches += 1
                            logging.info(f"Item {i} matches using improved prefiltering")
                            
                            # Log which keywords were found and in which fields
                            for field in item.keys():
                                if isinstance(item[field], str):
                                    for keyword in KEYWORDS:
                                        if keyword.lower() in item[field].lower():
                                            preview = item[field][:50] + "..." if len(item[field]) > 50 else item[field]
                                            logging.info(f"Keyword '{keyword}' found in field '{field}': '{preview}'")
                
                logging.info(f"Found {total_matches} matching items out of {checked_items} checked items using improved_title_contains_keywords()")
            
            return True
        else:
            logging.error(f"Failed to connect to feed endpoint. Status code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Error testing Woot API structure: {e}")
        logging.error(traceback.format_exc())
        return False

# Add catch-all route handlers
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    if path == 'health':
        return "OK", 200

    try:
        return check_woot_deals(request)
    except Exception as e:
        logging.error(f"Error handling request: {e}")
        logging.error(traceback.format_exc())
        # Report the crash before returning, so an unhandled exception still
        # reaches the user rather than only the logs.
        try:
            record_health_event("unhandled_exception", repr(e))
            report_run_health("failed", {"feed_items": 0})
        except Exception:
            logging.error("Could not report health for the unhandled exception")

        # Only ask for a retry if one could actually help. Once the feed has been
        # fetched, the Woot rate-limit budget is already spent, and a scheduler
        # retry would pay for a second full pagination at the worst moment.
        if _feed_was_fetched:
            logging.error("Crashed after the feed fetch; not asking for a retry")
            return "Internal error (already reported)", 200

        # Deliberately vague: this endpoint is reachable without authentication.
        return "Internal error", 500

# Keep the health endpoint for backward compatibility
@app.route('/health', methods=['GET'])
def health_check():
    return "OK", 200

# Start the server when run directly
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port)