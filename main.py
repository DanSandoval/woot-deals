import requests
import json
import logging
import smtplib
import ssl
import imaplib
import re
from email import message_from_bytes, policy as email_policy
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone
import os
from google.cloud import storage
import sys
import traceback
import time
from flask import Flask, request
import random
import html
import hashlib
import anthropic

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

# The SEED for the keyword list, not the live list. Production reads
# keywords.json from the bucket, which the user edits by emailing commands (see
# process_keyword_commands); this constant only fills that file the first time
# it is missing. After that, editing it here changes nothing in production.
DEFAULT_KEYWORDS = ["kindle", "ereader", "e-reader", "e-ink", "kobo", "nook", "eink",
                    "airtag", "air-tag", "mac mini", "3d printer", "3-d printer",
                    "montessori", "macbook air", "macbook pro", "mac studio"]
# Both AirTag spellings are listed for the same reason as ereader/e-reader:
# normalize_text flattens hyphens, so "air-tag" covers "Air Tag" and
# "Air-Tag" while "airtag" covers Apple's own one-word branding. Matching is
# substring, so "airtag" also picks up the "AirTags" plural on its own.
# "mac mini" needs one entry: flattening already turns "Mac-Mini" and the
# "apple-mac-mini-m4" slug into the same text. "3d printer" covers "3D Printer",
# "3D-Printer" and the plural, but "3-D Printer" flattens to "3 d printer", so
# that spelling is listed separately. Accessories named after the product
# ("Stand for Mac mini", "3D Printer Filament") match too, as AirTag cases do.
# "montessori" is the toy keyword: it is a descriptor sellers put in the title
# rather than a brand, so one entry covers every product ("Montessori Busy
# Board", "Montessori Toys for 1 Year Old") and the substring match picks up
# "Montessori-Style" and the possessive on its own. The three Mac laptops and
# desktops follow the "mac mini" rule: one entry each, with the same accessory
# caveat ("MacBook Pro 14 Sleeve" matches).


def normalize_text(text):
    """Lowercase and flatten hyphens so "e-reader", "e reader" and slug text all match."""
    return text.replace("-", " ").lower()


# The list every matcher in this process uses. A run replaces it with the
# bucket's copy; until then it holds the defaults, which is what the offline
# tests and the diagnostic endpoints match against.
_keywords = []
_normalized_keywords = []


def set_keywords(keywords):
    """Make `keywords` the live list for matching."""
    global _keywords, _normalized_keywords
    _keywords = list(keywords)
    _normalized_keywords = [normalize_text(k) for k in _keywords]


set_keywords(DEFAULT_KEYWORDS)


def matched_keywords(text):
    """Return the keywords present in a piece of text (empty when text is missing or not a string)."""
    if not isinstance(text, str) or not text:
        return []
    haystack = normalize_text(text)
    return [_keywords[i] for i, k in enumerate(_normalized_keywords) if k in haystack]

# Gmail configuration
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

# GCS configuration
BUCKET_NAME = os.environ.get("BUCKET_NAME")
SEEN_DEALS_FILENAME = "seen_deals.json"

# --- Keyword commands by email ------------------------------------------------
# The keyword list lives in the bucket and is edited by emailing the tracker's
# own Gmail account with a subject like "woot add lego". Each run reads the
# inbox over IMAP with the same app password it already sends mail with.
KEYWORDS_FILENAME = "keywords.json"
IMAP_HOST = "imap.gmail.com"
IMAP_TIMEOUT = 20  # seconds, per socket operation
# Addresses besides GMAIL_USER itself that may send commands, comma-separated.
# Kept out of the code because the repository is public.
COMMAND_SENDERS = os.environ.get("COMMAND_SENDERS", "")
# Gmail label put on every command once it has been handled, so it is never
# applied twice. Read/unread state is not used for this: opening the email on
# a phone before the next run would otherwise make the command vanish.
COMMAND_LABEL = "woot-processed"
COMMAND_LOOKBACK_DAYS = 3
MAX_COMMANDS_PER_RUN = 10    # verified commands applied per run
MAX_COMMAND_CANDIDATES = 50  # messages whose headers are read per run
MAX_KEYWORDS = 60
KEYWORD_MIN_CHARS = 3   # letters/digits; "tv" or "a" would match nearly everything
KEYWORD_MAX_CHARS = 40
# A keyword matching more than this share of live offers is refused. At that
# breadth it would text on almost every run and spend the detail-fetch budget
# on things nobody asked about. For scale, "refurbished" matches about 1%.
MAX_KEYWORD_MATCH_RATIO = 0.05
# --- Screening matches with Jev -------------------------------------------------
# Keywords match by substring, so they also catch accessories ("Case for Kindle
# Paperwhite") and words that merely contain a keyword ("Waste Ink" contains
# "e ink"). Jev, TypeSafe's judgment model, is asked whether each new match is
# really the product. Anything it rules out is still listed in the alert email,
# just not texted, so a wrong call costs a text, never a deal.
#
# Measured 2026-09-27 on testdata/jev_corpus.json: 120 labelled listings, 49
# live from Woot and 71 built as hard cases. With this model and threshold no
# wanted listing was set aside and every accessory and look-alike outside
# Montessori was. Outside Montessori the lowest wanted listing scored 0.90 and
# the highest non-wanted one 0.06. test_jev_live.py re-checks this. The threshold sits at the
# non-wanted end on purpose: an extra accessory text is cheap, a missed deal is
# not. Re-measure before changing JEV_MODEL, the questions or the descriptions.
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY")  # unset = no screening
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"  # pinned: the threshold was measured on this version
JEV_DROP_BELOW = 0.2
JEV_TIMEOUT = 10  # seconds per request
JEV_MAX_ATTEMPTS = 3
JEV_DETAIL_CHARS = 600  # per text field; Jev judges worse with long irrelevant text

JEV_PRODUCTS = {
    "ereader": "An e-reader or e-ink tablet: a device for reading ebooks or writing "
               "on an e-paper screen, such as an Amazon Kindle, Kobo, Barnes & Noble "
               "Nook, reMarkable or BOOX. New, refurbished or used, alone or in a "
               "bundle that includes the device.",
    "airtag": "Apple AirTag item trackers, single or multi-pack, alone or in a "
              "bundle that includes at least one AirTag.",
    "mac_mini": "An Apple Mac mini desktop computer, any year or configuration, "
                "new, refurbished or used, alone or in a bundle that includes it.",
    "mac_studio": "An Apple Mac Studio desktop computer, any configuration, new, "
                  "refurbished or used, alone or in a bundle that includes it.",
    "macbook_air": "An Apple MacBook Air laptop, any year or configuration, new, "
                   "refurbished or used, alone or in a bundle that includes it.",
    "macbook_pro": "An Apple MacBook Pro laptop, any year or configuration, new, "
                   "refurbished or used, alone or in a bundle that includes it.",
    "printer3d": "A 3D printer: the printing machine itself (filament or resin), "
                 "alone or in a bundle or combo that includes the printer.",
}
JEV_ACCESSORY_EXAMPLES = ("a case, cover, sleeve, skin, screen protector, charger, "
                          "cable, adapter, stand, dock, hub, mount, holder, keychain, "
                          "strap, filament, resin, nozzle, enclosure or replacement part")

# Which description judges which keyword, keyed by the normalized keyword so
# "air tag" typed into an email finds the "air-tag" entry. A keyword absent from
# here gets a description written by Claude instead (below), except
# "montessori", deliberately: it is a descriptor rather than a product, Jev
# scored real Montessori shelves as low as 0.11, and the word does not hide
# inside other words, so there is little to screen out and real deals to lose.
JEV_KEYWORD_PRODUCT = {normalize_text(k): product for k, product in {
    "kindle": "ereader", "ereader": "ereader", "e-reader": "ereader",
    "e-ink": "ereader", "eink": "ereader", "kobo": "ereader", "nook": "ereader",
    "remarkable": "ereader",  # not a default keyword; judged on real reMarkables
    "airtag": "airtag", "air-tag": "airtag",
    "mac mini": "mac_mini", "mac studio": "mac_studio",
    "macbook air": "macbook_air", "macbook pro": "macbook_pro",
    "3d printer": "printer3d", "3-d printer": "printer3d",
}.items()}
NEVER_SCREENED = frozenset({"montessori"})

# --- Jev screens for the other keywords -----------------------------------------
# Any other keyword gets its description from Claude, once, when it is added.
# Claude also writes sample listings the user would and would not want, and Jev
# scores the samples against the description. The screen goes on only if every
# wanted sample scores at least AUTO_SCREEN_MIN_KEPT, well clear of
# JEV_DROP_BELOW. That check stands in for the hand-labelled corpus behind the
# descriptions above. It catches a description too narrow for its own samples,
# but not a misreading of what the user meant. So the add reply shows Claude's
# reading and Jev's verdict on the offers live on Woot, and "woot unscreen"
# turns a screen off.
#
# Screens live in their own bucket file because they fail differently from the
# list. An unreadable keyword list stops the run. An unreadable screen file only
# means matches are texted unscreened, as before screens existed.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # unset = no new screens
KEYWORD_SCREENS_FILENAME = "keyword_screens.json"
AUTO_SCREEN_MODEL = "claude-opus-5-5"
AUTO_SCREEN_EFFORT = "low"  # a short, well-specified task; it has to fit the run budget
AUTO_SCREEN_TIMEOUT = 40    # seconds for the Claude call
AUTO_SCREEN_JEV_SECONDS = 15  # the self-check and live preview, about 20 Jev calls
AUTO_SCREEN_MIN_KEPT = 0.5
AUTO_SCREEN_MIN_SAMPLES = 4   # wanted samples needed for the self-check to mean anything
AUTO_SCREEN_MAX_SAMPLES = 8   # of each kind, to bound the Jev calls
AUTO_SCREEN_RETRY_HOURS = 2   # after a failed setup; doubles per failure, up to 48
LIVE_MATCH_LIST_LIMIT = 10    # live offers listed (and previewed) in an add reply

AUTO_SCREEN_INSTRUCTIONS = f"""\
You are setting up a filter for a personal deal tracker that watches Woot.com.

The user gives the tracker keywords. Whenever a new Woot listing contains a \
keyword, the user gets a text message. Matching is a plain substring search, \
case-insensitive, with hyphens read as spaces. So a keyword also matches \
accessories named after the product ("Case for Kindle Paperwhite") and longer \
words that happen to contain it ("wardrobe" contains "robe").

Before texting, the tracker asks a judgment model, Jev, about each matching \
listing: "Is `listing` selling `wanted_product` itself, or a bundle that \
includes it?" Listings Jev scores below {JEV_DROP_BELOW} are emailed instead of \
texted. A lost deal is much worse than an extra text, so `wanted_product` must \
cover everything the user could plausibly want under this keyword.

For the keyword you are given, return:

- screen: false when the keyword does not name a kind of product, so no \
description could say which matching listings are wanted: a descriptor or style \
("montessori", "vintage"), a condition ("refurbished"), a store section \
("clearance"), or a brand that spans unrelated kinds of product. Otherwise true.
- reading: one short sentence for the user's confirmation email. When screen \
is true, say in plain words what you take them to want. When false, say why the \
keyword cannot be screened.
- wanted_product: the description Jev receives, written like these:
  "{JEV_PRODUCTS['airtag']}"
  "{JEV_PRODUCTS['mac_mini']}"
  "{JEV_PRODUCTS['printer3d']}"
  Cover every variant, size, model and condition, multi-packs, and bundles that \
include the product. If the keyword could mean more than one kind of product, \
cover each of them.
- accessory_examples: the accessories, parts and consumables sold for this \
product, as one phrase like "a case, cover, charger, cable, stand or \
replacement part".
- keep: 6 listing titles, written the way Woot titles real offers, for \
products the user wants. Vary them: brand first, refurbished, multi-pack, \
bundle, and one long, awkward title. Each must contain the keyword.
- set_aside: 6 listing titles that contain the keyword but are not the \
product: accessories sold without it, and unrelated products whose names \
contain the keyword, including inside a longer word. Fewer is fine if few are \
plausible.

When screen is false, leave wanted_product, accessory_examples and both lists \
empty."""

AUTO_SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "screen": {"type": "boolean"},
        "reading": {"type": "string"},
        "wanted_product": {"type": "string"},
        "accessory_examples": {"type": "string"},
        "keep": {"type": "array", "items": {"type": "string"}},
        "set_aside": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["screen", "reading", "wanted_product", "accessory_examples",
                 "keep", "set_aside"],
    "additionalProperties": False,
}

KEYWORD_COMMAND_HELP = [
    "To change the list, email this address with one of these as the subject:",
    "  woot add <keyword>, <keyword>, ...",
    "  woot remove <keyword>, <keyword>, ...",
    "  woot list",
    "  woot unscreen <keyword>   (text every match, skipping Jev)",
]

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
# Keyword commands run between the feed and the detail fetch, so they leave the
# detail fetch its reserve plus one slow IMAP round trip.
COMMANDS_BUDGET_RESERVE = FEED_BUDGET_RESERVE + IMAP_TIMEOUT
# A Jev screen not set up during the commands is tried at the end of the run,
# which only needs to send the result email and report health afterwards.
AUTO_SCREEN_END_RESERVE = IMAP_TIMEOUT + 10
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
    "keyword_commands_failed", # inbox unreachable; the current list keeps working
    "keyword_command_rejected",# a command from an unverified sender was ignored
    "jev_unavailable",         # matches were texted unscreened; nothing was lost
    "keyword_screen_failed",   # a keyword's screen is not set up yet; retried later
    "keyword_screens_unreadable",  # Claude-described screens off; matches still texted
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

# Woot's catalogue turns over daily, so a full day of seeing nothing new means
# the feed is stale or the seen-index is wrong -- the pipeline looking alive
# while no longer actually observing anything, which is how the original bug
# presented. A single run with no new offers is perfectly normal.
#
# Measured in HOURS, not runs. This was "24 runs", which meant a day only while
# the schedule was hourly; the move to every 30 minutes on 2026-08-27 quietly
# halved it to twelve hours without anyone noticing, and it then fired on
# 2026-09-20 during an ordinary slow Sunday. Sixteen days of history put the
# normal daily quiet stretch at 8-9h and the observed maximum at 14.5h, so a day
# is both the rule's original intent and a comfortable margin over reality.
NO_NEW_ITEMS_HOURS = 24

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
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context()) as server:
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

def load_keywords():
    """
    Load the keyword list from Cloud Storage.

    Returns the list, or None if the file exists but cannot be used. A missing
    file is seeded from DEFAULT_KEYWORDS. None is fatal for the run, the same as
    an unreadable seen index: matching against the wrong list would record new
    offers as seen without checking them against the user's real keywords, and
    a seen offer never alerts.
    """
    try:
        if not storage_client:
            logging.error("Storage client not initialized")
            return None
        blob = storage_client.bucket(BUCKET_NAME).blob(KEYWORDS_FILENAME)
        if not blob.exists():
            logging.info(f"'{KEYWORDS_FILENAME}' does not exist; seeding it with "
                         f"{len(DEFAULT_KEYWORDS)} default keywords")
            save_keywords(DEFAULT_KEYWORDS)  # a failed seed is retried next run
            return list(DEFAULT_KEYWORDS)

        payload = json.loads(blob.download_as_text())
        keywords = payload.get("keywords") if isinstance(payload, dict) else None
        if (not isinstance(keywords, list) or not keywords
                or not all(isinstance(k, str) and k.strip() for k in keywords)):
            logging.error(f"'{KEYWORDS_FILENAME}' holds no usable keyword list")
            return None
        return keywords
    except Exception as e:
        logging.error(f"Error loading keywords: {e}")
        logging.error(traceback.format_exc())
        return None


def save_keywords(keywords):
    """Save the keyword list to Cloud Storage. Returns True on success."""
    try:
        if not storage_client:
            return False
        blob = storage_client.bucket(BUCKET_NAME).blob(KEYWORDS_FILENAME)
        blob.upload_from_string(
            json.dumps({"version": 1, "keywords": list(keywords),
                        "updated": datetime.now(timezone.utc).isoformat()}),
            content_type="application/json",
        )
        logging.info(f"Saved {len(keywords)} keywords to Cloud Storage")
        return True
    except Exception as e:
        logging.error(f"Error saving keywords: {e}")
        logging.error(traceback.format_exc())
        return False


def load_keyword_screens():
    """
    Load the Jev screens Claude described, keyed by normalized keyword.

    Returns {} when there are none yet, or None if the file exists but cannot
    be used. Unlike an unreadable keyword list this is not fatal: without
    screens, matches are texted unscreened, as they were before screens existed.
    """
    try:
        if not storage_client:
            return None
        blob = storage_client.bucket(BUCKET_NAME).blob(KEYWORD_SCREENS_FILENAME)
        if not blob.exists():
            return {}
        payload = json.loads(blob.download_as_text())
        screens = payload.get("screens") if isinstance(payload, dict) else None
        if not isinstance(screens, dict) or not all(isinstance(e, dict) for e in screens.values()):
            logging.error(f"'{KEYWORD_SCREENS_FILENAME}' holds no usable screens")
            return None
        return screens
    except Exception as e:
        logging.error(f"Error loading keyword screens: {e}")
        return None


def save_keyword_screens(screens):
    """
    Save the screens. Returns True on success. Refuses after a failed read, so
    a damaged file is left for a person to look at rather than overwritten.
    """
    if not _screens_writable:
        return False
    try:
        if not storage_client:
            return False
        blob = storage_client.bucket(BUCKET_NAME).blob(KEYWORD_SCREENS_FILENAME)
        blob.upload_from_string(
            json.dumps({"version": 1, "screens": screens,
                        "updated": datetime.now(timezone.utc).isoformat()}, indent=1),
            content_type="application/json",
        )
        return True
    except Exception as e:
        logging.error(f"Error saving keyword screens: {e}")
        logging.error(traceback.format_exc())
        return False


_COMMAND_RE = re.compile(r"^\s*woot\s+(add|remove|delete|list|help|unscreen)\b[\s:]*(.*)$",
                         re.IGNORECASE | re.DOTALL)

# Letters, digits and a little punctuation. Besides rejecting nonsense this
# keeps line breaks and control characters out of the logs and the reply.
_KEYWORD_CHARS = re.compile(r"^[a-z0-9][a-z0-9 &+'.-]*$")


def parse_keyword_command(subject):
    """
    Read a command from an email subject: ("add", ["lego", "switch 2"]).

    Returns None for anything else, including replies and forwards ("Re: woot
    add ..."), so ordinary mail that merely mentions Woot is never acted on.
    """
    match = _COMMAND_RE.match(subject or "")
    if not match:
        return None
    action = match.group(1).lower()
    if action == "delete":
        action = "remove"
    args = [a.strip() for a in re.split(r"[,;\r\n]", match.group(2)) if a.strip()]
    return action, args


def _printable(text, limit=KEYWORD_MAX_CHARS):
    """User-supplied text made safe to echo into a log line or reply."""
    return "".join(c for c in str(text) if c.isprintable())[:limit]


def clean_keyword(raw):
    """Return (keyword, None) for a usable keyword, or (None, reason) if refused."""
    keyword = " ".join(str(raw).strip().strip("\"'").lower().split())
    if not keyword:
        return None, "it is empty"
    if len(keyword) > KEYWORD_MAX_CHARS:
        return None, f"it is longer than {KEYWORD_MAX_CHARS} characters"
    if not _KEYWORD_CHARS.match(keyword):
        return None, "only letters, numbers, spaces and - & + ' . are allowed"
    if sum(c.isalnum() for c in keyword) < KEYWORD_MIN_CHARS:
        return None, (f"it is too short; a keyword needs at least {KEYWORD_MIN_CHARS} "
                      f"letters or numbers or it matches nearly everything")
    return keyword, None


def _feed_item_texts(item):
    """Every piece of a feed item's text the pre-filter matches keywords against."""
    for field in PREFILTER_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value:
            yield field, value
    categories = item.get("Categories")
    if isinstance(categories, list):
        yield "Categories", " ".join(c for c in categories if isinstance(c, str))


def items_mentioning(term, items):
    """
    Feed items the pre-filter would flag for `term`.

    Same fields as improved_title_contains_keywords, Categories included: a
    keyword like "tools" is rare in titles but names a whole category, and
    judging it on titles alone would let it flag every new Tools offer.
    """
    needle = normalize_text(term)
    return [item for item in items
            if any(needle in normalize_text(text) for _, text in _feed_item_texts(item))]


def apply_keyword_command(action, args, keywords, feed_items):
    """
    Apply one command to a copy of the list.

    Returns (new_keywords, report_lines); report_lines become the reply email.
    `feed_items` is this run's catalogue, used to refuse keywords so broad they
    would match a large share of Woot, and to show what a new keyword finds.
    """
    updated = list(keywords)
    lines = []

    if action == "add":
        if not args:
            lines.append('Nothing to add. Put the keyword after "woot add".')
        for raw in args:
            keyword, problem = clean_keyword(raw)
            if problem:
                lines.append(f'Not added "{_printable(raw)}": {problem}.')
                continue
            if normalize_text(keyword) in {normalize_text(k) for k in updated}:
                lines.append(f"Already on the list: {keyword}")
                continue
            if len(updated) >= MAX_KEYWORDS:
                lines.append(f'Not added "{keyword}": the list is full ({MAX_KEYWORDS} '
                             f'keywords). Remove one first.')
                continue
            live = items_mentioning(keyword, feed_items)
            if feed_items and len(live) > MAX_KEYWORD_MATCH_RATIO * len(feed_items):
                lines.append(f'Not added "{keyword}": it matches {len(live)} of the '
                             f'{len(feed_items)} offers on Woot right now, which is too '
                             f'broad to alert on. Try something more specific.')
                continue
            updated.append(keyword)
            lines.append(f"Added: {keyword}")
            if live:
                lines.append(f"  {len(live)} offers on Woot match it right now. They were "
                             f"already seen, so they will not alert; only new listings will:")
                lines += [f"  - {i.get('Title') or '(no title)'}  {i.get('Url') or ''}".rstrip()
                          for i in live[:LIVE_MATCH_LIST_LIMIT]]
                if len(live) > LIVE_MATCH_LIST_LIMIT:
                    lines.append(f"  ...and {len(live) - LIVE_MATCH_LIST_LIMIT} more")
            else:
                lines.append("  Nothing on Woot matches it right now.")

    elif action == "remove":
        if not args:
            lines.append('Nothing to remove. Put the keyword after "woot remove".')
        for raw in args:
            target = normalize_text(" ".join(raw.strip().strip("\"'").split()))
            found = [k for k in updated if normalize_text(k) == target]
            if not found:
                lines.append(f'Not on the list: "{_printable(raw)}"')
                continue
            if len(updated) == 1:
                lines.append(f'Not removed "{found[0]}": it is the last keyword, and an '
                             f'empty list would silently match nothing.')
                continue
            updated.remove(found[0])
            lines.append(f"Removed: {found[0]}")

    elif action == "list":
        lines.append("No changes.")

    else:  # help
        lines.append("No changes.")

    return updated, lines


def apply_unscreen(args, keywords):
    """
    Switch Jev off for keywords on the list, so every match is texted.
    Returns (screens, report_lines); screens is a changed copy.
    """
    screens = dict(_screens)
    lines = []
    if not args:
        lines.append('Nothing to unscreen. Put the keyword after "woot unscreen".')
    on_list = {normalize_text(k): k for k in keywords}
    for raw in args:
        norm = normalize_text(" ".join(raw.strip().strip("\"'").split()))
        keyword = on_list.get(norm)
        if keyword is None:
            lines.append(f'Not on the list: "{_printable(raw)}"')
        elif not _screens_writable:
            lines.append(f'Not unscreened "{keyword}": the screen file could not be read. '
                         f"Try again later.")
        elif norm in NEVER_SCREENED or (screens.get(norm) or {}).get("status") == "off":
            lines.append(f"Already unscreened: {keyword}")
        else:
            screens[norm] = {"status": "off", "keyword": keyword,
                             "reason": 'you sent "woot unscreen"',
                             "at": datetime.now(timezone.utc).isoformat()}
            lines.append(f"Unscreened: {keyword}. Every match for it will be texted. "
                         f"To screen it again, remove it and add it back.")
    return screens, lines


def forget_screens(keywords):
    """
    Drop the screens of removed keywords, so one added back is described
    afresh. Best effort: a stale entry only matters if the keyword returns.
    """
    norms = {normalize_text(k) for k in keywords}
    if not norms & set(_screens):
        return
    screens = {k: e for k, e in _screens.items() if k not in norms}
    if save_keyword_screens(screens):
        set_screens(screens)


def _gmail_dmarc_pass(raw_headers, sender):
    """
    Whether Gmail itself verified that `sender` really sent this message.

    Gmail prepends its own Authentication-Results header on arrival, so the
    topmost one is Gmail's verdict; any a sender wrote themselves sits below it.

    Even Gmail's header carries sender-chosen text: the envelope address is
    copied into the SPF comment and smtp.mailfrom, so a MAIL FROM of
    <dmarc=pass@evil.example> would satisfy a plain substring search. The
    verdict is therefore parsed, not searched: quoted strings and comments go
    first, then exactly one clause may start with "dmarc=", and it must be
    "dmarc=pass header.from=<sender's domain>". The raw header is used
    (compat32) so no encoded-word decoding can introduce text either.
    """
    msg = message_from_bytes(raw_headers or b"", policy=email_policy.compat32)
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False
    verdict = " ".join(str(results[0]).split())
    verdict = re.sub(r'"(?:[^"\\]|\\.)*"', '""', verdict)
    previous = None
    while previous != verdict:  # comments may nest
        previous, verdict = verdict, re.sub(r"\([^()]*\)", " ", verdict)
    authserv, *clauses = [c.strip().lower() for c in verdict.split(";")]
    if authserv != "mx.google.com":
        return False
    dmarc = [c.split() for c in clauses if c.startswith("dmarc=")]
    if len(dmarc) != 1:
        return False
    domain = sender.rsplit("@", 1)[-1]
    return dmarc[0][0] == "dmarc=pass" and f"header.from={domain}" in dmarc[0][1:]


def canonical_address(address):
    """
    An address in the form Gmail delivers to.

    Gmail ignores dots and anything after "+" in the local part, and rewrites
    the From header of mail this account sends: mail sent as
    "firstlast@gmail.com" can arrive From "first.last@gmail.com". Both are the
    same mailbox, and nobody else can own either spelling.
    """
    local, _, domain = str(address).strip().lower().rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.split("+", 1)[0].replace(".", ""), "gmail.com"
    return f"{local}@{domain}" if local and domain else ""


# Gmail system labels arrive as \Sent, or quoted as "\\Sent".
_SENT_LABEL = re.compile(rb'(?:^|[\s(])"?\\{1,2}Sent"?(?=[\s)])')


def verified_command_sender(msg, labels, raw_headers):
    """
    The sender of a command email if it provably came from an allowed address,
    else None.

    The From header is trivially forged, so it is never trusted alone. A
    stranger who could command the tracker could empty the keyword list and
    the user would quietly stop getting alerts.
    - From the tracker's own account: Gmail must have filed the message under
      \\Sent, which only happens to mail this account actually sent. An outside
      message claiming this From address cannot get that label.
    - From another address in COMMAND_SENDERS: Gmail's own
      Authentication-Results must say dmarc=pass for that address's domain.
    """
    sender = canonical_address(parseaddr(str(msg.get("From", "")))[1])
    if not sender:
        return None
    if GMAIL_USER and sender == canonical_address(GMAIL_USER):
        return sender if _SENT_LABEL.search(labels or b"") else None
    allowed = {canonical_address(a) for a in COMMAND_SENDERS.split(",") if a.strip()}
    if sender in allowed and _gmail_dmarc_pass(raw_headers, sender):
        return sender
    return None


def _send_keyword_reply(to, lines, keywords):
    """Tell the sender what their command did. Best effort."""
    if TYPESAFE_API_KEY:
        width = max((len(k) for k in keywords), default=0)
        listed = [f"  {k.ljust(width)}  {screen_status(k)}" for k in keywords]
    else:
        listed = [f"  {k}" for k in keywords]
    body = "\n".join(
        lines + ["", f"Keyword list ({len(keywords)}):"] + listed
        + [""] + KEYWORD_COMMAND_HELP)
    # The subject must not look like a command, or a reply to the tracker's own
    # address would be read back in as one.
    msg = MIMEText(body, "plain")
    msg["Subject"] = "Woot keywords"
    msg["From"] = GMAIL_USER
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=IMAP_TIMEOUT,
                              context=ssl.create_default_context()) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        logging.error(f"Could not send keyword reply: {e}")
        return False


def _imap_ok(response, what):
    typ, data = response
    if typ != "OK":
        raise RuntimeError(f"IMAP {what} failed: {typ} {data!r}"[:300])
    return data


def _process_mailbox(imap, feed_items):
    """Apply pending commands in an open IMAP session. Returns how many were applied."""
    imap.create(COMMAND_LABEL)  # Gmail makes it a label; answers NO once it exists
    _imap_ok(imap.select("INBOX"), "select")

    # Gmail's own search narrows this to likely commands. It is only a first
    # pass; parse_keyword_command decides, and anything it rejects is left
    # completely untouched -- this is a personal inbox.
    query = (f"subject:(woot (add OR remove OR delete OR list OR help OR unscreen)) "
             f"newer_than:{COMMAND_LOOKBACK_DAYS}d -label:{COMMAND_LABEL}")
    data = _imap_ok(imap.uid("SEARCH", "X-GM-RAW", f'"{query}"'), "search")
    uids = (data[0] or b"").split() if data else []

    keywords = list(_keywords)
    applied = 0
    # Oldest first, so commands apply in the order they were sent.
    for uid in uids[:MAX_COMMAND_CANDIDATES]:
        if applied >= MAX_COMMANDS_PER_RUN:
            logging.info("More keyword commands are waiting; they will be handled next run")
            break
        if budget_remaining() < COMMANDS_BUDGET_RESERVE:
            logging.warning("Run budget is short; remaining keyword commands wait for next run")
            break

        # Headers only. Message bodies are never downloaded.
        fetched = _imap_ok(imap.uid("FETCH", uid, "(BODY.PEEK[HEADER])"), "fetch")
        raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
        if not raw:
            continue
        msg = message_from_bytes(raw, policy=email_policy.default)
        command = parse_keyword_command(str(msg.get("Subject", "")))
        if command is None:
            continue

        labels = _imap_ok(imap.uid("FETCH", uid, "(X-GM-LABELS)"), "label fetch")
        label_bytes = b" ".join(p if isinstance(p, bytes) else p[0] for p in labels if p)
        sender = verified_command_sender(msg, label_bytes, raw)
        if sender is None:
            # The address is the sender's own text. Reduced to plain address
            # characters it cannot forge a WOOT_HEALTH line for the monitoring
            # metric to count.
            claimed = re.sub(r"[^A-Za-z0-9@._+-]", "_",
                             parseaddr(str(msg.get("From", "")))[1])[:80]
            logging.warning(f"Ignored a keyword command from unverified sender {claimed}")
            record_health_event("keyword_command_rejected",
                                f"ignored a command from unverified sender {claimed}")
            # Labelled anyway so it is not re-examined every run. No reply:
            # answering unverified mail would let anyone make this account
            # send email to an address of their choosing.
            imap.uid("STORE", uid, "+X-GM-LABELS", f"({COMMAND_LABEL})")
            continue

        action, args = command
        if action == "unscreen":
            screens, lines = apply_unscreen(args, keywords)
            if screens != _screens:
                if not save_keyword_screens(screens):
                    raise RuntimeError("could not save the keyword screens")
                set_screens(screens)
        else:
            updated, lines = apply_keyword_command(action, args, keywords, feed_items)
            if updated != keywords:
                if not save_keywords(updated):
                    # Leave the email unlabelled so the next run applies it again.
                    raise RuntimeError("could not save the keyword list")
                added = [k for k in updated if k not in keywords]
                forget_screens([k for k in keywords if k not in updated])
                keywords = updated
                set_keywords(keywords)
                # Uses part of the run budget; commands left over wait for the
                # next run, as they do when the inbox is slow.
                for keyword in added:
                    screen_lines = screen_lines_for_added(keyword, feed_items)
                    if screen_lines:
                        lines += [""] + screen_lines
        logging.info(f"Keyword command '{action}' with {len(args)} argument(s) applied; "
                     f"the list now holds {len(keywords)} keywords")

        _send_keyword_reply(sender, lines, keywords)
        # Labelled last. If this fails the command is re-applied next run, which
        # is harmless: adding or removing the same keyword twice changes nothing.
        _imap_ok(imap.uid("STORE", uid, "+X-GM-LABELS", f"({COMMAND_LABEL})"), "label")
        applied += 1

    return applied


def process_keyword_commands(feed_items):
    """
    Apply keyword commands emailed to the tracker. Returns how many were applied.

    Never raises: an unreachable inbox is recorded as a notable event and the
    run carries on with the list it already has. Deal alerts must not depend on
    this.
    """
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        return 0
    if budget_remaining() < COMMANDS_BUDGET_RESERVE:
        logging.warning("Run budget is short; keyword commands wait for next run")
        return 0
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT,
                                 ssl_context=ssl.create_default_context())
        imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        return _process_mailbox(imap, feed_items)
    except Exception as e:
        logging.error(f"Could not process keyword commands: {e}")
        logging.error(traceback.format_exc())
        record_health_event("keyword_commands_failed", repr(e))
        return 0
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


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

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context()) as server:
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


def _apply_staleness_check(status, kinds, state, metrics, now):
    """
    Flag a feed that has stopped changing.

    A single run with no new offers is normal; a full day of them means the feed
    is stale or the seen-index is wrong -- the pipeline looking alive while no
    longer actually observing anything.

    Elapsed time is the thing this rule was always about, so it is what gets
    measured. Counting runs instead made the threshold depend on the schedule,
    and when the cadence changed the same number silently came to mean half as
    long. A timestamp cannot drift that way.

    Note this check only runs while the status is still "ok": a run already
    reporting a problem has a better explanation than "nothing new appeared".
    That also means a persistently noisy check will suppress this one, which is
    how it sat dormant through the September 2026 alert flood.
    """
    if status != "ok" or "new_items" not in metrics:
        return status, kinds, state

    # Retired by the switch to wall-clock time; dropped so the state file stops
    # carrying a value nothing reads.
    state.pop("consecutive_no_new_runs", None)

    if int(metrics["new_items"]) != 0:
        state["last_new_item_seen"] = now.isoformat()
        return status, kinds, state

    try:
        quiet_for = now - datetime.fromisoformat(state["last_new_item_seen"])
    except (KeyError, TypeError, ValueError):
        # Nothing usable to measure from -- first run on a new state file, or a
        # corrupted value. Start the clock rather than alert about a gap whose
        # length is unknown.
        state["last_new_item_seen"] = now.isoformat()
        return status, kinds, state

    if quiet_for < timedelta(hours=NO_NEW_ITEMS_HOURS):
        return status, kinds, state

    record_health_event(
        "feed_not_changing",
        f"no new offers for {quiet_for.total_seconds() / 3600:.1f}h; "
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

    status, kinds, state = _apply_staleness_check(status, kinds, state, metrics, now)

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

def _plain_text(value, limit):
    """HTML-stripped, whitespace-collapsed text, cut to `limit` characters."""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v)
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()
    return text[:limit]


# keyword_screens.json, keyed by normalized keyword. An entry's status is "on"
# (Claude's description passed the self-check), "off" (no screen: the user sent
# "woot unscreen", Claude found no product to describe, or the self-check
# failed) or "error" (setup failed; retried later). No entry means not set up yet.
_screens = {}
_screens_writable = True  # False after a failed read, so nothing overwrites the file


def set_screens(screens):
    """Make `screens` the live set. None means the file could not be read."""
    global _screens, _screens_writable
    _screens_writable = screens is not None
    _screens = dict(screens or {})


def _auto_product_id(norm_keyword):
    """A stable Jev question key for a keyword's own description."""
    slug = re.sub(r"[^a-z0-9]+", "_", norm_keyword).strip("_")[:30]
    return f"kw_{slug}_{hashlib.sha1(norm_keyword.encode()).hexdigest()[:6]}"


def _jev_spec(norm_keyword):
    """
    (product, wanted_product, accessory examples) for a keyword, or None when
    Jev does not judge it.

    A Claude-written description is used only while it is "on" and was checked
    against the Jev model in use: a model change invalidates the check, just as
    it would the threshold measured for the descriptions above.
    """
    entry = _screens.get(norm_keyword) or {}
    if entry.get("status") == "off":
        return None
    product = JEV_KEYWORD_PRODUCT.get(norm_keyword)
    if product:
        return product, JEV_PRODUCTS[product], JEV_ACCESSORY_EXAMPLES
    wanted, accessories = entry.get("wanted_product"), entry.get("accessory_examples")
    if (entry.get("status") == "on" and entry.get("jev_model") == JEV_MODEL
            and isinstance(wanted, str) and wanted and isinstance(accessories, str)):
        return _auto_product_id(norm_keyword), wanted, accessories
    return None


def _needs_screen(norm_keyword):
    """Whether a keyword is waiting for Claude to describe it (or describe it again)."""
    if not (TYPESAFE_API_KEY and ANTHROPIC_API_KEY and _screens_writable):
        return False
    if norm_keyword in JEV_KEYWORD_PRODUCT or norm_keyword in NEVER_SCREENED:
        return False
    entry = _screens.get(norm_keyword) or {}
    if entry.get("status") == "off":
        return False
    if entry.get("status") == "on":
        return _jev_spec(norm_keyword) is None  # checked on another Jev model, or damaged
    return True  # never set up, or failed (_retry_due decides when to try again)


def screen_status(keyword):
    """How a keyword is screened, in words for the reply email."""
    norm = normalize_text(keyword)
    if TYPESAFE_API_KEY and _jev_spec(norm) is not None:
        return "screened" if norm in JEV_KEYWORD_PRODUCT else "screened (auto)"
    return "screen pending" if _needs_screen(norm) else "not screened"


def _jev_products_for(deal):
    """
    {product: (wanted_product, accessory examples)} for the keywords this deal
    matched.

    None means "do not judge it": a matched keyword has no screen (Montessori,
    one switched off, or one not set up yet), or nothing matched. Such a deal
    is always texted -- one keyword Jev cannot judge is enough to vouch for it.
    """
    keywords = set()
    for field in DEAL_MATCH_FIELDS:
        keywords.update(matched_keywords(deal.get(field)))
    if not keywords:
        return None
    specs = {}
    for keyword in keywords:
        spec = _jev_spec(normalize_text(keyword))
        if spec is None:
            return None
        specs[spec[0]] = spec[1:]
    return dict(sorted(specs.items()))


def _jev_questions(products):
    """
    The questions Jev is asked, per product. Only `wanted` decides anything, but
    all three are asked because that is the exact request the threshold was
    measured with; the accessory score also explains a drop in the email.
    """
    questions = {}
    for product, (wanted, accessory_examples) in products.items():
        questions[f"wanted__{product}"] = {
            "type": "noul",
            "instructions": {
                "wanted_product": wanted,
                "question": "Is `listing` selling `wanted_product` itself, or a "
                            "bundle that includes it?",
            },
            "criteria": {
                "true": "The listing sells the product described in "
                        "`wanted_product`, on its own or with extras included.",
                "false": "The listing sells something else: an accessory or part "
                         "for that product sold without it, or an unrelated product "
                         "whose name only resembles it.",
            },
        }
        questions[f"accessory__{product}"] = {
            "type": "noul",
            "instructions": {
                "wanted_product": wanted,
                "question": "Is `listing` an accessory, consumable or part for "
                            f"`wanted_product` ({accessory_examples}), sold "
                            "without the product itself?",
            },
        }
        questions[f"unrelated__{product}"] = {
            "type": "noul",
            "instructions": {
                "wanted_product": wanted,
                "question": "Is `listing` for a product that has nothing to do with "
                            "`wanted_product`, being neither that product nor an "
                            "accessory for it?",
            },
        }
    return questions


def _jev_state(deal, feed_item):
    """What Jev sees of the offer. Categories only exist on the feed's copy."""
    listing = {"title": deal.get("Title") or (feed_item or {}).get("Title") or ""}
    if isinstance(deal.get("Subtitle"), str) and deal["Subtitle"].strip():
        listing["subtitle"] = deal["Subtitle"]
    categories = (feed_item or {}).get("Categories") or deal.get("Categories")
    if isinstance(categories, list):
        categories = [c for c in categories if isinstance(c, str)]
        if categories:
            listing["categories"] = categories
    for field, key in (("Features", "features"), ("WriteUpBody", "writeup")):
        text = _plain_text(deal.get(field), JEV_DETAIL_CHARS)
        if text:
            listing[key] = text
    return {"listing": listing}


def _jev_scores(state, products):
    """
    Ask Jev about one offer. Returns {product: {"wanted": p, "accessory": p}}.

    Raises on any failure; the caller keeps the deal. 429 and 529 (overloaded)
    are retried with backoff, as TypeSafe's API docs ask.
    """
    delay = 1.0
    for attempt in range(1, JEV_MAX_ATTEMPTS + 1):
        try:
            response = requests.request(
                "POST", JEV_ENDPOINT,
                headers={"Authorization": f"Bearer {TYPESAFE_API_KEY}"},
                json={"state": state, "model": JEV_MODEL,
                      "questions": _jev_questions(products)},
                timeout=JEV_TIMEOUT,
            )
            problem = None if response.status_code == 200 else \
                f"HTTP {response.status_code}: {response.text[:200]}"
            retryable = response.status_code in (429, 500, 502, 503, 504, 529)
        except requests.RequestException as e:
            problem, retryable = type(e).__name__, True
        if problem is None:
            break
        if (not retryable or attempt == JEV_MAX_ATTEMPTS
                or budget_remaining() < delay + JEV_TIMEOUT + 20):
            raise RuntimeError(f"Jev request failed: {problem}")
        time.sleep(delay)
        delay *= 2

    answers = response.json().get("answers") or {}
    scores = {}
    for product in products:
        pair = {q: float(answers[f"{q}__{product}"]["noul"]) for q in ("wanted", "accessory")}
        if not all(0.0 <= p <= 1.0 for p in pair.values()):  # also rejects NaN
            raise ValueError(f"Jev returned an out-of-range score for {product}: {pair}")
        scores[product] = pair
    return scores


def screen_matches(deals, feed_items):
    """
    Split confirmed matches into (to_text, filtered_out) using Jev.

    filtered_out holds (deal, wanted_score, accessory_score) for offers Jev is
    confident are not the product -- an accessory, or a word that only contains
    a keyword. They are still emailed, never silently dropped.

    Fails open everywhere: with no API key, on any error or timeout, once the
    run budget is short, or for a keyword that has no screen, the deal is
    texted exactly as it would have been without this filter.
    """
    if not TYPESAFE_API_KEY or not deals:
        return list(deals), []

    feed_by_id = {str(i.get("OfferId") or i.get("Id")): i for i in feed_items}
    to_text, filtered, failures = [], [], 0
    for deal in deals:
        deal_id = str(deal.get("Id") or deal.get("OfferId") or "unknown")
        products = _jev_products_for(deal)
        if products is None:
            to_text.append(deal)
            continue
        if budget_remaining() < JEV_TIMEOUT + 20:
            logging.warning(f"Run budget too short to ask Jev about {deal_id}; texting it")
            to_text.append(deal)
            continue
        try:
            scores = _jev_scores(_jev_state(deal, feed_by_id.get(deal_id)), products)
        except Exception as e:
            failures += 1
            logging.warning(f"Jev could not judge {deal_id}, texting it: {e}")
            to_text.append(deal)
            continue

        best = max(s["wanted"] for s in scores.values())
        logging.info(f"Jev scored {deal_id} {scores}")
        # Dropped only when EVERY product it matched says no, so a bundle of
        # two tracked things survives if either one is really in it.
        if best < JEV_DROP_BELOW:
            accessory = max(s["accessory"] for s in scores.values())
            filtered.append((deal, best, accessory))
        else:
            to_text.append(deal)

    if failures:
        record_health_event("jev_unavailable",
                            f"Jev could not judge {failures} of {len(deals)} matches; "
                            f"they were texted unfiltered")
    return to_text, filtered


def _describe_keyword(keyword):
    """
    Ask Claude what `keyword` is after. Returns the cleaned answer; raises on
    any failure, including an answer with too few usable samples to check.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=AUTO_SCREEN_TIMEOUT,
                                 max_retries=0)  # a failed setup is retried by a later run
    response = client.beta.messages.create(
        model=AUTO_SCREEN_MODEL,
        max_tokens=8000,
        system=AUTO_SCREEN_INSTRUCTIONS,
        messages=[{"role": "user", "content": f"Keyword: {keyword}"}],
        output_config={"effort": AUTO_SCREEN_EFFORT,
                       "format": {"type": "json_schema", "schema": AUTO_SCREEN_SCHEMA}},
        # If a safety classifier declines, the API reruns the request on the
        # model Anthropic recommends instead of returning the refusal.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Claude stopped early ({response.stop_reason})")
    answer = json.loads(next(b.text for b in response.content if b.type == "text"))

    norm = normalize_text(keyword)

    def samples(name, exclude=()):
        # A sample without the keyword in it could never reach the screen.
        titles = []
        for raw in answer.get(name) or []:
            title = _printable(" ".join(str(raw).split()), 160)
            if norm in normalize_text(title) and title not in titles and title not in exclude:
                titles.append(title)
        return titles[:AUTO_SCREEN_MAX_SAMPLES]

    keep = samples("keep")
    cleaned = {
        "screen": answer.get("screen") is True,
        "reading": _printable(" ".join(str(answer.get("reading") or "").split()), 300),
        "wanted_product": _printable(" ".join(str(answer.get("wanted_product") or "").split()), 800),
        "accessory_examples": _printable(
            " ".join(str(answer.get("accessory_examples") or "").split()), 300),
        "keep": keep,
        "set_aside": samples("set_aside", exclude=keep),
    }
    if cleaned["screen"]:
        if not cleaned["wanted_product"]:
            raise ValueError("Claude returned no product description")
        if len(keep) < AUTO_SCREEN_MIN_SAMPLES:
            raise ValueError(f"Claude returned {len(keep)} usable sample listings, "
                             f"fewer than the {AUTO_SCREEN_MIN_SAMPLES} the check needs")
        if not cleaned["accessory_examples"]:
            cleaned["accessory_examples"] = "a case, cover, charger, cable, stand or replacement part"
    return cleaned


def _jev_wanted(state, product, spec):
    """Jev's `wanted` score for one listing against one description. Raises on failure."""
    return _jev_scores(state, {product: spec})[product]["wanted"]


def set_up_screen(keyword, feed_items, reserve):
    """
    Describe `keyword` with Claude, check the description with Jev, store the
    result, and preview it on the live offers that match the keyword.

    Returns (entry, report_lines), or None when the run has too little budget
    left to try: then nothing is spent or stored and the keyword stays pending.
    `reserve` is the budget the caller still needs afterwards.
    """
    if budget_remaining() < reserve + AUTO_SCREEN_TIMEOUT + AUTO_SCREEN_JEV_SECONDS:
        return None

    norm = normalize_text(keyword)
    product = _auto_product_id(norm)
    now = datetime.now(timezone.utc).isoformat()
    try:
        answer = _describe_keyword(keyword)
        if not answer["screen"]:
            entry = {"status": "off", "keyword": keyword,
                     "reason": answer["reading"] or "Claude found no one kind of product to describe",
                     "model": AUTO_SCREEN_MODEL, "at": now}
        else:
            spec = (answer["wanted_product"], answer["accessory_examples"])
            check = {}
            for kind in ("keep", "set_aside"):
                check[kind] = {}
                for title in answer[kind]:
                    if budget_remaining() < reserve + JEV_TIMEOUT:
                        raise RuntimeError("the run budget ran out during the self-check")
                    check[kind][title] = _jev_wanted({"listing": {"title": title}}, product, spec)
            entry = {"status": "on", "keyword": keyword, "reading": answer["reading"],
                     "wanted_product": spec[0], "accessory_examples": spec[1],
                     "check": check, "model": AUTO_SCREEN_MODEL, "jev_model": JEV_MODEL,
                     "at": now}
            title, lowest = min(check["keep"].items(), key=lambda kv: kv[1])
            if lowest < AUTO_SCREEN_MIN_KEPT:
                entry["status"] = "off"
                entry["reason"] = (f'in a self-check Jev scored "{title}", a listing it '
                                   f"should keep, only {lowest:.2f}, too close to setting "
                                   f"real deals aside")
    except Exception as e:
        previous = _screens.get(norm) or {}
        attempts = previous.get("attempts", 0) + 1 if previous.get("status") == "error" else 1
        reason = _printable(f"{type(e).__name__}: {e}", 200)
        entry = {"status": "error", "keyword": keyword, "reason": reason,
                 "attempts": attempts, "at": now}
        logging.warning(f"Could not set up a Jev screen for '{keyword}': {reason}")
        record_health_event("keyword_screen_failed",
                            f"no Jev screen yet for '{keyword}' (attempt {attempts}); "
                            f"its matches are texted unscreened")

    screens = dict(_screens)
    screens[norm] = entry
    if not save_keyword_screens(screens):
        logging.error(f"Could not save the Jev screen for '{keyword}'; it applies to this run only")
    set_screens(screens)
    logging.info(f"Jev screen for '{keyword}': {entry['status']}")

    # The preview only informs the reply; it decides nothing.
    preview = []
    if entry["status"] == "on":
        spec = (entry["wanted_product"], entry["accessory_examples"])
        for item in items_mentioning(keyword, feed_items)[:LIVE_MATCH_LIST_LIMIT]:
            if budget_remaining() < reserve + JEV_TIMEOUT:
                break
            try:
                preview.append((item.get("Title") or "(no title)",
                                _jev_wanted(_jev_state(item, item), product, spec)))
            except Exception as e:
                logging.warning(f"Jev could not preview an offer for '{keyword}': {e}")
    return entry, _screen_report(keyword, entry, preview)


def _screen_report(keyword, entry, preview=()):
    """The reply-email lines describing one keyword's screen."""
    if entry["status"] == "on":
        keep = list(entry["check"]["keep"].values())
        aside = list(entry["check"]["set_aside"].values())
        caught = sum(1 for s in aside if s < JEV_DROP_BELOW)
        lines = [f'Jev screen for "{keyword}": on',
                 f"  Reading: {entry['reading'] or entry['wanted_product']}",
                 f"  Self-check: the lowest of {len(keep)} listings it should keep scored "
                 f"{min(keep):.2f} (needs {AUTO_SCREEN_MIN_KEPT}); it set aside {caught} of "
                 f"{len(aside)} look-alikes."]
        if preview:
            lines.append("  Its verdict on the offers on Woot that match it right now:")
            lines += [f"    {'keep     ' if w >= JEV_DROP_BELOW else 'set aside'}  {w:.2f}  "
                      f"{_printable(title, 80)}" for title, w in preview]
        lines += ["  Set-aside matches are still emailed, just not texted. If the reading is",
                  f'  wrong, send "woot unscreen {keyword}" and every match will be texted.']
        return lines
    if entry["status"] == "off":
        return [f'Jev screen for "{keyword}": off, so every match will be texted.',
                f"  Why: {entry['reason']}"]
    return [f'Jev screen for "{keyword}": not set up yet; every match is texted until it is.',
            f"  Why: {entry['reason']}. It is retried automatically."]


def screen_lines_for_added(keyword, feed_items):
    """Set up a screen for a keyword just added by email. Returns the reply lines."""
    norm = normalize_text(keyword)
    if not TYPESAFE_API_KEY:
        return []
    if norm in JEV_KEYWORD_PRODUCT:
        return [f'Jev screen for "{keyword}": on, with a hand-tested description.']
    if not _needs_screen(norm):
        return [f'Jev screen for "{keyword}": none, so every match will be texted.']
    result = set_up_screen(keyword, feed_items, reserve=COMMANDS_BUDGET_RESERVE)
    if result is None:
        return [f'Jev screen for "{keyword}": being set up; the result follows in a '
                f"separate email, usually within the hour."]
    return result[1]


def _retry_due(entry, now):
    """Whether a failed setup has waited long enough to try again."""
    if not entry or entry.get("status") != "error":
        return True
    try:
        failed_at = datetime.fromisoformat(entry["at"])
    except (KeyError, TypeError, ValueError):
        return True
    hours = min(AUTO_SCREEN_RETRY_HOURS * 2 ** (entry.get("attempts", 1) - 1), 48)
    return now >= failed_at + timedelta(hours=hours)


def set_up_pending_screen(feed_items):
    """
    Set up at most one screen still missing: for a keyword added when the run
    had no time to spare, one whose setup failed earlier, one checked against
    an older Jev model, or one added before screens existed. Emails the owner
    once it is settled. Never raises.
    """
    try:
        now = datetime.now(timezone.utc)
        for keyword in _keywords:
            norm = normalize_text(keyword)
            if not (_needs_screen(norm) and _retry_due(_screens.get(norm), now)):
                continue
            result = set_up_screen(keyword, feed_items, reserve=AUTO_SCREEN_END_RESERVE)
            # A failure is not mailed: it is retried, and noted in the health line.
            if result and result[0]["status"] != "error":
                _send_keyword_reply(GMAIL_USER, result[1], _keywords)
            return
    except Exception as e:
        logging.error(f"Setting up a pending Jev screen failed: {e}")
        logging.error(traceback.format_exc())
        record_health_event("keyword_screen_failed", repr(e)[:200])


DEAL_MATCH_FIELDS = ("Title", "Subtitle", "WriteUpBody", "Features", "Snippet", "Slug")

# Feed-item fields the pre-filter reads, plus Categories. In practice the live
# feed fills only Title and Categories (Slug and Subtitle were null on all
# 11,097 items on 2026-09-27); the rest are kept in case the feed changes.
# Slug would carry the product name even when Title is generic; normalize_text
# flattens its hyphens so multi-word keywords still match.
PREFILTER_FIELDS = (
    "Title", "title", "Description", "description", "Subtitle", "subtitle",
    "Snippet", "snippet", "Summary", "summary", "Name", "name",
    "ProductName", "productName", "WriteUpBody", "writeUpBody",
    "Features", "features", "Slug", "slug",
)


def is_matching_deal(deal):
    """Check whether a detailed offer matches our keywords."""
    deal_id = deal.get("Id", deal.get("OfferId", "unknown"))

    for field in DEAL_MATCH_FIELDS:
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

def send_notifications(deals, filtered=()):
    """
    Send the alert text and email for new deals. Returns True on success.

    `filtered` holds (deal, wanted_score, accessory_score) for matches Jev ruled
    out. They are listed at the end of the email so nothing disappears
    unseen, but never texted: a text about a Kindle case is the noise the
    filter exists to remove. With only filtered matches, just the email goes.
    """
    if not deals and not filtered:
        logging.info("No deals to send notifications for. Skipping.")
        return False

    logging.info(f"Preparing to send notifications for {len(deals)} deals "
                 f"and {len(filtered)} filtered-out matches")
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

        if deals and not formatted:
            logging.error("No deals could be formatted; nothing to send")
            return False

        if filtered:
            text_parts.append(
                "Filtered out as accessories or look-alikes (not texted):\n" + "\n".join(
                    f"  {d.get('Title') or 'No Title'} - {d.get('Url') or 'No URL'} "
                    f"(is the product: {w:.2f}, accessory: {a:.2f})"
                    for d, w, a in filtered))
            html_parts.append(
                "<h3>Filtered out as accessories or look-alikes (not texted)</h3><ul>"
                + "".join(
                    f'<li><a href="{html.escape(d.get("Url") or "", quote=True)}">'
                    f'{html.escape(d.get("Title") or "No Title")}</a> '
                    f"(is the product: {w:.2f}, accessory: {a:.2f})</li>"
                    for d, w, a in filtered)
                + "</ul>")
            if not deals:
                email_msg.replace_header(
                    "Subject", f"Woot: {len(filtered)} match(es) filtered out, nothing texted")

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
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context()) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)

            # Send text message first
            if formatted:
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

    for field, text in _feed_item_texts(item):
        hits = matched_keywords(text)
        if hits:
            item_id = item.get("OfferId", item.get("Id", "unknown"))
            logging.info(f"Pre-filter hit on {item_id}: {hits} in field '{field}'")
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

    # Same reasoning: a run that matched against the wrong keywords would mark
    # every new offer seen without checking it against the user's real list.
    keywords = load_keywords()
    if keywords is None:
        record_health_event("keywords_unreadable",
                            f"could not read {KEYWORDS_FILENAME} from {BUCKET_NAME}")
        report_run_health("failed", metrics)
        return "Error: could not read the keyword list", 503
    set_keywords(keywords)
    screens = load_keyword_screens()
    if screens is None:
        record_health_event("keyword_screens_unreadable",
                            f"could not read {KEYWORD_SCREENS_FILENAME}; matches for "
                            f"keywords Claude described are texted unscreened")
    set_screens(screens)

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

    # Emailed keyword changes are applied before matching, so a keyword added
    # this run already catches new offers this run. Needs the catalogue in hand
    # to refuse keywords that would match a large share of it.
    metrics["commands"] = process_keyword_commands(feed_items)
    metrics["keywords"] = len(_keywords)

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

    # Step 3b: have Jev set aside accessories and look-alikes. They are still
    # emailed, and still recorded as seen below, exactly like texted deals.
    to_text, filtered_out = screen_matches(matching_deals, feed_items)
    metrics["jev_filtered"] = len(filtered_out)

    # Step 4: notify
    notified = False
    if matching_deals:
        logging.info(f"Found {len(matching_deals)} new matching deals "
                     f"({len(filtered_out)} filtered out). Sending notifications.")
        notified = send_notifications(to_text, filtered_out)
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

    # Step 6: with whatever budget is left, set up one Jev screen still missing.
    # Last, so it only spends time this run's own work did not need.
    set_up_pending_screen(feed_items)

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
    # Title, Subtitle and Slug only, as it always has been: widening the fields
    # would move the counts this observation period is collecting.
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
                                        for keyword in _keywords:
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
                                    for keyword in _keywords:
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