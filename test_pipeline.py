#!/usr/bin/env python3
"""
Offline tests for the deal-checking pipeline.

Simulates the Woot API (including its token-bucket rate limit, which is what
silently truncated the feed) and Cloud Storage, so the whole path from feed
fetch through notification can be exercised without credentials.

Run: python test_pipeline.py
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("WOOT_API_KEY", "test-key")
os.environ.setdefault("GMAIL_USER", "sender@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "app-password")
os.environ.setdefault("EMAIL_RECIPIENT", "5551234567@example.com")
os.environ.setdefault("BUCKET_NAME", "test-bucket")

import main  # noqa: E402


PAGE_SIZE = 100
# The live API reports one more page than it actually serves: it advertises
# TotalPages=51 but page 51 answers 404. SERVED_PAGES is the real count.
REPORTED_PAGES = 51
SERVED_PAGES = 50

# Woot caps the All feed at 5000 items while the category feeds together reach
# ~12400, so All alone shows about 40% of the catalogue. The fake mirrors that:
# All serves the first 5000 offers and each category adds distinct ones on top.
# Gourmet and Wootoff are empty, as they were when measured against the live API
# -- an empty feed is a legitimate answer, not a failure.
CATEGORY_SLICES = {
    "Clearance": (5000, 2000),
    "Computers": (7000, 500),
    "Electronics": (7500, 500),
    "Featured": (8000, 10),
    "Home": (8010, 2000),
    "Gourmet": (0, 0),
    "Shirts": (10010, 200),
    "Sports": (10210, 1000),
    "Tools": (11210, 790),
    "Wootoff": (0, 0),
}
# What a healthy multi-feed run should merge to.
CATALOG_SIZE = SERVED_PAGES * PAGE_SIZE + sum(n for _, n in CATEGORY_SLICES.values())


def make_item(index):
    """A feed item shaped like the ones the live API returns."""
    if index == 4200:
        title, slug = "Kindle Paperwhite 16GB", "kindle-paperwhite-16gb"
    elif index == 4300:
        title, slug = "Kobo Clara HD", "kobo-clara-hd-ereader"
    elif index == 12:
        title, slug = "Refurb E-Reader Bundle", "refurb-e-reader-bundle"
    else:
        title, slug = f"Widget {index}", f"widget-{index}"
    return {
        "OfferId": f"offer-{index:05d}",
        "Title": title,
        "Subtitle": None,  # the live feed really does send nulls here
        "Slug": slug,
        "Categories": ["Electronics"],
        "Url": f"https://woot.com/offers/{slug}",
        "SalePrice": 99.99,
        "ListPrice": 149.99,
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeWootApi:
    """Token-bucket rate limiter matching the behaviour seen in production logs."""

    def __init__(self, burst=10, refill_per_second=1.0, enforce_limit=True,
                 serve_full_feed=True):
        self.burst = burst
        self.refill_per_second = refill_per_second
        self.enforce_limit = enforce_limit
        # The live API returns the ENTIRE feed when `page` is omitted. On by
        # default because that is what production does; turn it off to force the
        # paginated fallback.
        self.serve_full_feed = serve_full_feed
        self.tokens = float(burst)
        self.clock = 0.0
        self.calls = []
        self.rate_limited = 0

    def feed_requests(self):
        return [u for _, u in self.calls if "/feed/" in u]

    # -- fake clock, so tests do not actually sleep --------------------------
    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def request(self, method, url, headers=None, **kwargs):
        # refill based on elapsed time since the last call
        elapsed = self.clock - getattr(self, "_last_clock", 0.0)
        self._last_clock = self.clock
        self.tokens = min(self.burst, self.tokens + elapsed * self.refill_per_second)
        self.calls.append((method, url))

        if self.enforce_limit:
            if self.tokens < 1:
                self.rate_limited += 1
                return FakeResponse(429, {"message": "Too Many Requests"})
            self.tokens -= 1

        if "/feed/" in url:
            name = url.split("/feed/")[1].split("?")[0]
            page = int(url.split("page=")[1]) if "page=" in url else None

            if name != "All":
                start, count = CATEGORY_SLICES.get(name, (0, 0))
                pages = max(1, -(-count // PAGE_SIZE))  # ceil
                if page is None:
                    # Categories always answer the non-paginated form in full.
                    items = [make_item(start + i) for i in range(count)]
                else:
                    if page > pages or count == 0:
                        return FakeResponse(404, None, text='"NotFound"')
                    lo = (page - 1) * PAGE_SIZE
                    items = [make_item(start + lo + i)
                             for i in range(min(PAGE_SIZE, count - lo))]
                return FakeResponse(200, {
                    "Items": items, "MarketingName": name, "TotalPages": pages,
                })

            if page is None and self.serve_full_feed:
                # Non-paginated mode: one response carrying the whole feed, while
                # TotalPages still advertises the paginated count.
                items = [make_item(i) for i in range(SERVED_PAGES * PAGE_SIZE)]
                return FakeResponse(200, {
                    "Items": items,
                    "MarketingName": "All",
                    "TotalPages": REPORTED_PAGES,
                })
            page = page or 1
            if page > SERVED_PAGES:
                return FakeResponse(404, None, text='"NotFound"')
            start = (page - 1) * PAGE_SIZE
            items = [make_item(start + i) for i in range(PAGE_SIZE)]
            return FakeResponse(200, {
                "Items": items,
                "MarketingName": "All",
                "TotalPages": REPORTED_PAGES,
            })

        if url.endswith("/getoffers"):
            ids = json.loads(kwargs["data"])
            offers = []
            for offer_id in ids:
                index = int(offer_id.split("-")[1])
                item = make_item(index)
                offers.append({
                    "Id": offer_id,
                    "Title": item["Title"],
                    "Url": item["Url"],
                    "WriteUpBody": None,
                    "Items": [{"SalePrice": 99.99, "ListPrice": 149.99}],
                })
            return FakeResponse(200, offers)

        return FakeResponse(404, None, text="not found")


class FakeBlob:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def exists(self):
        return self.name in self.store

    def download_as_text(self):
        return self.store[self.name]

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = data


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return FakeBlob(self.store, name)


class FakeStorageClient:
    def __init__(self, store):
        self.store = store

    def bucket(self, name):
        return FakeBucket(self.store)


GMAIL_DMARC_PASS = ("mx.google.com; dkim=pass header.i=@gmail.com; spf=pass "
                    "smtp.mailfrom=me@gmail.com; dmarc=pass (p=NONE sp=QUARANTINE "
                    "dis=NONE) header.from=gmail.com")


class FakeGmail:
    """The tracker's Gmail inbox, as far as keyword commands touch it over IMAP."""

    def __init__(self):
        self.messages = {}  # uid -> {"raw": header bytes, "labels": set}
        self.next_uid = 1
        self.fail_login = False

    def add(self, subject, sender=None, sent=True, auth_results=()):
        """
        Deliver a message. `sent` marks it as sent BY this account (Gmail's
        \\Sent label), which a message from anyone else can never carry.
        """
        lines = [f"Authentication-Results: {a}" for a in auth_results]
        lines += [f"From: {sender or main.GMAIL_USER}", f"To: {main.GMAIL_USER}",
                  f"Subject: {subject}", "Message-ID: <m@example.com>"]
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = {"raw": ("\r\n".join(lines) + "\r\n\r\n").encode(),
                              "labels": {"\\Sent"} if sent else set()}
        return uid

    def processed(self, uid):
        return main.COMMAND_LABEL in self.messages[uid]["labels"]

    def connect(self, host, port=993, timeout=None, ssl_context=None):
        self.connect_kwargs = {"timeout": timeout, "ssl_context": ssl_context}
        return FakeImapSession(self)


class FakeImapSession:
    def __init__(self, gmail):
        self.gmail = gmail

    def login(self, user, password):
        if self.gmail.fail_login:
            raise main.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"authenticated"]

    def create(self, name):
        return "NO", [b"[ALREADYEXISTS] Duplicate folder name"]

    def select(self, mailbox):
        return "OK", [str(len(self.gmail.messages)).encode()]

    def logout(self):
        return "BYE", [b"logging out"]

    def uid(self, command, *args):
        if command == "SEARCH":
            # Ignores the Gmail query on purpose: whatever the search lets
            # through, the tracker itself must still refuse non-commands.
            pending = [u for u, m in sorted(self.gmail.messages.items())
                       if main.COMMAND_LABEL not in m["labels"]]
            return "OK", [b" ".join(str(u).encode() for u in pending)]
        uid = int(args[0])
        msg = self.gmail.messages[uid]
        # Strict on purpose. Anything else could change the owner's mail:
        # BODY[...] or RFC822 would mark it read and download the body, and
        # X-GM-LABELS without "+" would replace its labels, archiving it.
        if command == "FETCH" and args[1] == "(X-GM-LABELS)":
            # Gmail quotes system labels with an escaped backslash: ("\\Sent").
            labels = b" ".join(
                (b'"\\\\' + l[1:].encode() + b'"') if l.startswith("\\") else l.encode()
                for l in sorted(msg["labels"]))
            return "OK", [f"{uid} (X-GM-LABELS (".encode() + labels + f") UID {uid})".encode()]
        if command == "FETCH" and args[1] == "(BODY.PEEK[HEADER])":
            head = f"{uid} (UID {uid} BODY[HEADER] {{{len(msg['raw'])}}}".encode()
            return "OK", [(head, msg["raw"]), b")"]
        if command == "STORE" and args[1] == "+X-GM-LABELS":
            msg["labels"].add(args[2].strip("()"))
            return "OK", [b""]
        raise AssertionError(f"IMAP {command} {args[1:]} would alter the owner's mailbox")


class PipelineTestBase(unittest.TestCase):
    """Shared fakes: Woot API with its rate limit, Cloud Storage, and the alert channel."""

    def setUp(self):
        self.api = FakeWootApi()
        self.store = {}
        self.sent = []
        self.alerts = []
        self.gmail = FakeGmail()
        self.replies = []
        self.filtered = []

        main._last_request_time = 0.0
        main._run_deadline = None

        # All really is pinned at Woot's 5000-item ceiling in production, and the
        # fixture mirrors that. Seed it as already-known so ordinary runs are
        # quiet -- alerting on a permanently capped feed every run would make the
        # signal worthless. The newly-capped tests exercise the change detection.
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps(
            {"capped_feeds": ["All"]})

        patches = [
            mock.patch.object(main.requests, "request", self.api.request),
            mock.patch.object(main.time, "monotonic", self.api.monotonic),
            mock.patch.object(main.time, "sleep", self.api.sleep),
            mock.patch.object(main, "storage_client", FakeStorageClient(self.store)),
            mock.patch.object(main, "send_notifications", self._send),
            mock.patch.object(main, "send_alert", self._alert),
            # Every run checks the inbox for keyword commands. Default to an
            # empty one, and capture the replies, so no test touches the network.
            mock.patch.object(main.imaplib, "IMAP4_SSL", self.gmail.connect),
            mock.patch.object(main.smtplib, "SMTP_SSL", self._smtp),
            # A developer machine may have a TypeSafe key in its environment;
            # screening is switched on only by the tests that exercise it.
            mock.patch.object(main, "TYPESAFE_API_KEY", None),
            mock.patch.object(main, "ANTHROPIC_API_KEY", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # A run replaces the live list with the bucket's copy; start every test
        # from the defaults and put them back afterwards.
        main.set_keywords(main.DEFAULT_KEYWORDS)
        self.addCleanup(main.set_keywords, main.DEFAULT_KEYWORDS)
        main.set_screens({})
        self.addCleanup(main.set_screens, {})

    def _smtp(self, *args, **kwargs):
        self.smtp_kwargs = kwargs
        server = mock.MagicMock()
        server.__enter__ = mock.Mock(return_value=server)
        server.__exit__ = mock.Mock(return_value=False)
        server.send_message.side_effect = self.replies.append
        return server

    def _send(self, deals, filtered=()):
        self.sent.append(list(deals))
        self.filtered.append(list(filtered))
        return True

    def _alert(self, subject, body):
        self.alerts.append((subject, body))
        return True

    def run_check(self):
        """Run one pass; returns (body, http_status)."""
        main._last_request_time = 0.0
        return main.check_woot_deals(None)

    def health_line(self):
        """
        Run one pass and return its machine-readable summary line.

        Cloud Monitoring keys off this exact string, so tests about which tier an
        event lands in have to read it rather than the alert: a notable event is
        deliberately invisible to self.alerts.
        """
        with self.assertLogs(level="INFO") as captured:
            self.run_check()
        lines = [r.getMessage() for r in captured.records
                 if r.getMessage().startswith(main.HEALTH_MARKER + " ")]
        self.assertTrue(lines, "the run emitted no health summary line")
        return lines[-1]

    def health_state(self):
        return json.loads(self.store.get(main.HEALTH_STATE_FILENAME, "{}"))


class PipelineTest(PipelineTestBase):
    # -- keyword matching ----------------------------------------------------

    def test_normalization_matches_hyphen_and_space_forms(self):
        self.assertEqual(main.matched_keywords("Kindle Paperwhite"), ["kindle"])
        self.assertEqual(main.matched_keywords("An E-Reader"), ["e-reader"])
        # Hyphen flattening lets a slug match the same as prose.
        self.assertEqual(main.matched_keywords("kobo-clara-hd-ereader"),
                         ["ereader", "kobo"])
        self.assertEqual(main.matched_keywords(None), [])
        self.assertEqual(main.matched_keywords(123), [])

    def test_airtag_spellings_all_match(self):
        """Woot sellers write AirTag several ways; all of them must match."""
        for title in ["Apple AirTag 4 Pack",
                      "Apple AirTag (1 Pack)",
                      "Apple Air Tag Bluetooth Tracker",
                      "Apple Air-Tag 4-Pack",
                      "AirTags 4-Pack Bluetooth Item Finder",
                      "apple airtag leather loop"]:
            with self.subTest(title=title):
                self.assertTrue(
                    main.matched_keywords(title),
                    f"{title!r} should have matched an AirTag keyword")

    def test_real_sellout_airtag_listing_matches(self):
        """
        A listing that really appeared on Woot:
        sellout.woot.com/offers/4-pack-apple-airtags-1st-gen-3

        Note it is a SELLOUT offer. Those reach the tracker only through the
        Clearance feed, not through All, so this is exactly the kind of deal
        the multi-feed change exists to make visible.
        """
        item = {
            "OfferId": "real-1",
            "Title": "4-Pack: Apple AirTags (1st Gen)",
            "Subtitle": None,
            "Slug": "4-pack-apple-airtags-1st-gen-3",
            "Categories": ["Electronics"],
            "Url": "https://sellout.woot.com/offers/4-pack-apple-airtags-1st-gen-3",
        }
        self.assertTrue(main.improved_title_contains_keywords(item))
        self.assertEqual(main.matched_keywords(item["Slug"]), ["airtag"])

    def test_airtag_keywords_do_not_match_unrelated_offers(self):
        for title in ["Cordless Air Compressor",
                      "Gift Tag Assortment, 50 Count",
                      "Air Fryer 6qt"]:
            with self.subTest(title=title):
                self.assertFalse(main.matched_keywords(title), title)

    def test_mac_mini_spellings_all_match(self):
        for title in ["Apple Mac mini M2 8GB 256GB",
                      "Apple Mac Mini (2023)",
                      "Apple Mac-Mini Desktop",
                      "apple-mac-mini-m4-16gb-512gb"]:
            with self.subTest(title=title):
                self.assertEqual(main.matched_keywords(title), ["mac mini"])

    def test_3d_printer_spellings_all_match(self):
        for title in ["Creality Ender 3 V3 SE 3D Printer",
                      "Bambu Lab A1 Mini 3D-Printer",
                      "Resin 3D Printers, 2-Pack",
                      "anycubic-kobra-2-neo-3d-printer"]:
            with self.subTest(title=title):
                self.assertEqual(main.matched_keywords(title), ["3d printer"])
        self.assertEqual(main.matched_keywords("FlashForge 3-D Printer"),
                         ["3-d printer"])

    def test_montessori_toy_spellings_all_match(self):
        for title in ["Montessori Busy Board for Toddlers",
                      "Montessori Toys for 1 Year Old, 6-Pack",
                      "Wooden Montessori-Style Stacking Rings",
                      "montessori-wooden-sensory-board"]:
            with self.subTest(title=title):
                self.assertEqual(main.matched_keywords(title), ["montessori"])

    def test_mac_laptop_and_studio_keywords_match(self):
        for title, keyword in [("Apple MacBook Air 13-inch M2", "macbook air"),
                               ("apple-macbook-air-15-m3-8gb", "macbook air"),
                               ("Apple MacBook Pro 14-inch M3 Pro", "macbook pro"),
                               ("Apple MacBook-Pro 16 (Refurbished)", "macbook pro"),
                               ("Apple Mac Studio M2 Max 32GB", "mac studio")]:
            with self.subTest(title=title):
                self.assertEqual(main.matched_keywords(title), [keyword])

    def test_new_keywords_do_not_match_unrelated_offers(self):
        for title in ["Apple Studio Display 27-inch",
                      "Apple iMac 24-inch M3",
                      "HP LaserJet Pro Printer",
                      "Sony 3D Blu-ray Player",
                      "Compact Mini Fridge",
                      "Wooden Toy Blocks, 100-Piece Set",
                      "Melissa & Doug Shape Sorter"]:
            with self.subTest(title=title):
                self.assertFalse(main.matched_keywords(title), title)

    def test_prefilter_tolerates_null_fields(self):
        item = make_item(4200)
        self.assertIsNone(item["Subtitle"])
        self.assertTrue(main.improved_title_contains_keywords(item))
        self.assertFalse(main.improved_title_contains_keywords(make_item(7)))

    # -- feed pagination -----------------------------------------------------
    # These exercise the paginated fallback specifically, so they call it
    # directly. Going through fetch_feed() would fetch all 11 feeds and test
    # merging rather than paging.

    def test_feed_fetches_every_page_despite_rate_limit(self):
        main.start_run_budget()
        items, complete = main._fetch_feed_paginated()
        self.assertTrue(complete)
        self.assertEqual(len(items), PAGE_SIZE * SERVED_PAGES)
        # Normalised ID field is present for downstream code.
        self.assertEqual(items[0]["Id"], items[0]["OfferId"])

    def test_trailing_404_is_end_of_feed_not_a_failure(self):
        """The API advertises 51 pages but serves 50; the 404 must not read as truncation."""
        main.start_run_budget()
        items, complete = main._fetch_feed_paginated()
        self.assertTrue(complete)
        self.assertIn(("GET", f"{main.FEED_ENDPOINT}?page={SERVED_PAGES + 1}"), self.api.calls)

    def test_404_before_any_items_is_a_failure(self):
        main.start_run_budget()
        with mock.patch.object(main.requests, "request",
                               lambda *a, **k: FakeResponse(404, None, text='"NotFound"')):
            items, complete = main._fetch_feed_paginated()
        self.assertFalse(complete)
        self.assertEqual(items, [])

    def _feed_with_override(self, page_no, response):
        real_request = self.api.request

        def request(method, url, **kwargs):
            if f"page={page_no}" in url:
                return response
            return real_request(method, url, **kwargs)

        return request

    def test_empty_page_mid_feed_is_a_short_read_not_a_clean_end(self):
        """A blank page 3 of 51 means the feed came back short; it must not read as complete."""
        main.start_run_budget()
        blank = FakeResponse(200, {"Items": [], "TotalPages": REPORTED_PAGES})
        with mock.patch.object(main.requests, "request",
                               self._feed_with_override(3, blank)):
            items, complete = main._fetch_feed_paginated()
        self.assertFalse(complete)
        self.assertEqual(len(items), PAGE_SIZE * 2)

    def test_empty_page_at_the_advertised_end_is_a_clean_finish(self):
        main.start_run_budget()
        blank = FakeResponse(200, {"Items": [], "TotalPages": REPORTED_PAGES})
        with mock.patch.object(main.requests, "request",
                               self._feed_with_override(REPORTED_PAGES, blank)):
            items, complete = main._fetch_feed_paginated()
        self.assertTrue(complete)

    def test_malformed_items_list_is_not_a_clean_end(self):
        main.start_run_budget()
        bad = FakeResponse(200, {"Items": None, "TotalPages": REPORTED_PAGES})
        with mock.patch.object(main.requests, "request",
                               self._feed_with_override(3, bad)):
            items, complete = main._fetch_feed_paginated()
        self.assertFalse(complete)

    def test_404_mid_feed_is_a_failure_not_end_of_feed(self):
        """Only a 404 at the advertised last page means end of feed."""
        main.start_run_budget()
        missing = FakeResponse(404, None, text='"NotFound"')
        with mock.patch.object(main.requests, "request",
                               self._feed_with_override(3, missing)):
            items, complete = main._fetch_feed_paginated()
        self.assertFalse(complete)
        self.assertEqual(len(items), PAGE_SIZE * 2)

    def test_feed_reports_incomplete_when_api_keeps_failing(self):
        main.start_run_budget()
        with mock.patch.object(main.requests, "request",
                               lambda *a, **k: FakeResponse(429, {"message": "Too Many Requests"})):
            items, complete = main._fetch_feed_paginated()
        self.assertFalse(complete)
        self.assertEqual(items, [])

    def test_unpaced_requests_hit_the_limit_but_backoff_recovers(self):
        """
        Regression guard for the original bug: unpaced pagination drains the
        token bucket and the API starts answering 429. The old code treated the
        first 429 as end-of-feed and returned a truncated list; now the backoff
        waits it out and still returns every page.
        """
        main.start_run_budget()
        with mock.patch.object(main, "MIN_REQUEST_INTERVAL", 0.0):
            items, complete = main._fetch_feed_paginated()

        self.assertGreater(self.api.rate_limited, 0, "expected the fake API to rate limit")
        self.assertTrue(complete)
        self.assertEqual(len(items), PAGE_SIZE * SERVED_PAGES)

    def test_pacing_avoids_the_rate_limit_entirely(self):
        main.start_run_budget()
        main.fetch_feed()
        self.assertEqual(self.api.rate_limited, 0)

    # -- seen-deals state ----------------------------------------------------

    def test_seen_deals_round_trip_and_legacy_upgrade(self):
        self.store[main.SEEN_DEALS_FILENAME] = json.dumps(["a", "b", "c"])
        loaded = main.load_seen_deals()
        self.assertEqual(set(loaded), {"a", "b", "c"})

        main.save_seen_deals(loaded)
        payload = json.loads(self.store[main.SEEN_DEALS_FILENAME])
        self.assertEqual(payload["version"], 2)
        self.assertEqual(set(main.load_seen_deals()), {"a", "b", "c"})

    def test_load_failure_is_distinguishable_from_empty(self):
        with mock.patch.object(main, "storage_client", None):
            self.assertIsNone(main.load_seen_deals())
        self.assertEqual(main.load_seen_deals(), {})  # bucket has no file yet

    def test_prune_drops_stale_entries_only(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        seen = {
            "fresh": now.isoformat(),
            "stale": (now - timedelta(days=main.SEEN_DEALS_RETENTION_DAYS + 1)).isoformat(),
        }
        self.assertEqual(set(main.prune_seen_deals(seen)), {"fresh"})

    # -- end to end ----------------------------------------------------------

    def test_full_run_finds_matches_and_records_state(self):
        result, status = self.run_check()
        self.assertEqual(status, 200)

        self.assertEqual(len(self.sent), 1)
        titles = sorted(d["Title"] for d in self.sent[0])
        self.assertEqual(titles, ["Kindle Paperwhite 16GB",
                                  "Kobo Clara HD",
                                  "Refurb E-Reader Bundle"])
        self.assertIn("complete=True", result)

        seen = json.loads(self.store[main.SEEN_DEALS_FILENAME])["deals"]
        self.assertEqual(len(seen), CATALOG_SIZE)

    def test_second_run_does_not_re_notify(self):
        self.run_check()
        self.sent.clear()
        result, _ = self.run_check()
        self.assertEqual(self.sent, [])
        self.assertIn("new matching deals: 0", result)

    def test_failed_notification_leaves_deals_unseen_for_retry(self):
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            self.run_check()

        seen = json.loads(self.store[main.SEEN_DEALS_FILENAME])["deals"]
        self.assertNotIn("offer-04200", seen)

        # Next run retries them.
        self.run_check()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0]), 3)

    def test_offers_omitted_by_the_detail_api_are_not_marked_seen(self):
        real_request = self.api.request

        def request(method, url, **kwargs):
            response = real_request(method, url, **kwargs)
            if url.endswith("/getoffers") and response.status_code == 200:
                # Drop one offer from the response, as the live API does for
                # expired or region-locked items.
                trimmed = [o for o in response.json() if o["Id"] != "offer-04200"]
                return FakeResponse(200, trimmed)
            return response

        with mock.patch.object(main.requests, "request", request):
            self.run_check()

        seen = json.loads(self.store[main.SEEN_DEALS_FILENAME])["deals"]
        self.assertNotIn("offer-04200", seen,
                         "an offer the detail API never returned must stay unseen")

    def test_run_aborts_when_state_cannot_be_read(self):
        with mock.patch.object(main, "load_seen_deals", lambda: None):
            result, status = self.run_check()
        self.assertIn("could not read seen-deals state", result)
        self.assertEqual(status, 503, "a hard failure must not look green to Cloud Scheduler")
        self.assertEqual(self.sent, [])
        self.assertTrue(self.alerts, "the user must be told the run could not start")


class MultiFeedTest(PipelineTestBase):
    """
    Every feed is fetched in ONE request each and merged into one catalogue.

    Two failures drive this. Polling 51 pages hourly needed 1224 requests/day
    against a 1000/day quota, so the feed died every evening; and All is capped
    at 5000 items, hiding ~60% of the catalogue including the Clearance and
    Sellout offers where discounted e-readers appear.
    """

    def test_every_feed_costs_exactly_one_request(self):
        items, complete = main.fetch_feed()
        self.assertTrue(complete)
        self.assertEqual(len(self.api.feed_requests()), len(main.FEED_NAMES),
                         "one request per feed, no pagination")
        self.assertEqual(main._feed_stats["feeds_ok"], len(main.FEED_NAMES))

    def test_merged_catalogue_is_bigger_than_the_all_feed_alone(self):
        items, complete = main.fetch_feed()
        self.assertTrue(complete)
        self.assertEqual(len(items), CATALOG_SIZE)
        self.assertGreater(len(items), SERVED_PAGES * PAGE_SIZE,
                           "categories must recover offers All cannot reach")

    def test_offers_appearing_in_several_feeds_are_deduplicated(self):
        items, _ = main.fetch_feed()
        ids = [i["OfferId"] for i in items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_feeds_are_not_failures(self):
        # Gourmet and Wootoff really are empty; an empty feed is an answer.
        items, complete = main.fetch_feed()
        self.assertTrue(complete)
        self.assertEqual(main._feed_stats["feeds_failed"], 0)

    def test_ids_are_normalised_on_the_single_request_path_too(self):
        items, _ = main.fetch_feed()
        self.assertTrue(all(i["Id"] == i["OfferId"] for i in items))

    def test_healthy_multi_feed_run_raises_no_problems(self):
        self.run_check()
        kinds = [e["kind"] for e in main._health_events]
        self.assertNotIn("feed_coverage_partial", kinds)
        self.assertNotIn("feed_incomplete", kinds)
        self.assertNotIn("feed_too_small", kinds)
        self.assertEqual(self.alerts, [])

    def test_one_failing_feed_makes_the_run_incomplete(self):
        # A partial catalogue must never let unseen offers be marked seen.
        real = self.api.request

        def request(method, url, **kwargs):
            if "/feed/Sports" in url:
                return FakeResponse(500, {"message": "boom"})
            return real(method, url, **kwargs)

        main.start_run_budget()
        with mock.patch.object(main.requests, "request", request):
            items, complete = main.fetch_feed()
        self.assertFalse(complete, "a missing feed means an incomplete read")
        self.assertGreater(main._feed_stats["feeds_failed"], 0)

    def test_partial_coverage_is_reported(self):
        real = self.api.request

        def request(method, url, **kwargs):
            if "/feed/Tools" in url:
                return FakeResponse(500, {"message": "boom"})
            return real(method, url, **kwargs)

        with mock.patch.object(main.requests, "request", request):
            self.run_check()
        self.assertIn("feed_coverage_partial",
                      [e["kind"] for e in main._health_events])

    def _cap_feed(self, feed_name, count=5000):
        """Make one feed answer at Woot's 5000-item ceiling."""
        real = self.api.request

        def request(method, url, **kwargs):
            if f"/feed/{feed_name}" in url and "page=" not in url:
                return FakeResponse(200, {
                    "Items": [make_item(900000 + i) for i in range(count)],
                    "MarketingName": feed_name, "TotalPages": 51,
                })
            return real(method, url, **kwargs)

        return request

    def test_capped_feeds_are_reported_every_run(self):
        # Visibility even when nothing is wrong: a capped feed is otherwise
        # invisible, since the run still says 11/11 read and complete.
        with mock.patch.object(main.requests, "request", self._cap_feed("Home")):
            main.start_run_budget()
            main.fetch_feed()
        self.assertIn("Home", main.capped_feeds())

    def test_a_feed_newly_reaching_the_cap_is_flagged(self):
        with mock.patch.object(main.requests, "request",
                               self._cap_feed("Electronics")):
            self.run_check()
        self.assertIn("feed_newly_capped",
                      [e["kind"] for e in main._health_events])

    def test_an_already_capped_feed_does_not_re_alert(self):
        # Home, Clearance and All are permanently capped in production. If those
        # alerted every run the signal would be worthless within a day.
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps(
            {"capped_feeds": ["All", "Home"]})
        with mock.patch.object(main.requests, "request", self._cap_feed("Home")):
            self.run_check()
        self.assertNotIn("feed_newly_capped",
                         [e["kind"] for e in main._health_events])

    def test_a_feed_below_the_warn_ratio_is_not_capped(self):
        # Electronics sits near 20% of the ceiling; it must stay quiet.
        with mock.patch.object(main.requests, "request",
                               self._cap_feed("Electronics", count=900)):
            main.start_run_budget()
            main.fetch_feed()
        self.assertNotIn("Electronics", main.capped_feeds())

    # -- the deadband --------------------------------------------------------
    # Home sat within a few dozen items of the 4500 entry line for ten days and
    # re-reported itself as newly capped four times as it drifted across. These
    # pin the gap that stops that without hiding a real crossing.

    def _drifting(self):
        """A feed between the clear line (4250) and the entry line (4500)."""
        return self._cap_feed("Home", count=4300)

    def test_a_feed_drifting_below_entry_stays_capped_and_stays_quiet(self):
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps(
            {"capped_feeds": ["All", "Home"]})
        with mock.patch.object(main.requests, "request", self._drifting()):
            self.run_check()
        # Still counted as capped, because it has not cleared the lower line...
        self.assertIn("Home", self.health_state()["capped_feeds"])
        # ...and therefore never re-announces itself.
        self.assertNotIn("feed_newly_capped",
                         [e["kind"] for e in main._health_events])

    def test_the_same_size_does_not_newly_cap_a_feed_that_was_clear(self):
        # The gap must be directional: 4300 holds a feed in, but cannot pull an
        # uncapped one across. Otherwise the deadband would just move the line.
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps(
            {"capped_feeds": ["All"]})
        with mock.patch.object(main.requests, "request", self._drifting()):
            self.run_check()
        self.assertNotIn("Home", self.health_state()["capped_feeds"])
        self.assertNotIn("feed_newly_capped",
                         [e["kind"] for e in main._health_events])

    def test_clearing_the_lower_line_releases_the_feed(self):
        # The deadband must not be a one-way latch: a feed that genuinely empties
        # has to leave the set, or it can never be reported as capped again.
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps(
            {"capped_feeds": ["All", "Home"]})
        with mock.patch.object(main.requests, "request",
                               self._cap_feed("Home", count=4000)):
            self.run_check()
        self.assertNotIn("Home", self.health_state()["capped_feeds"])

    def test_a_genuine_crossing_still_reports(self):
        # The point of the gap is to keep this signal meaningful, not to mute it.
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps(
            {"capped_feeds": ["All"]})
        with mock.patch.object(main.requests, "request",
                               self._cap_feed("Home", count=4900)):
            self.run_check()
        self.assertIn("feed_newly_capped",
                      [e["kind"] for e in main._health_events])

    def test_regressing_to_all_only_coverage_trips_the_size_floor(self):
        # The most likely silent failure: categories stop working and only All
        # answers. 5000 items looks plausible, so the floor sits above it.
        self.assertGreater(main.FEED_SIZE_FLOOR, SERVED_PAGES * PAGE_SIZE)

    def test_short_single_response_falls_back_to_pagination(self):
        # With serve_full_feed off, All's un-paged call returns just one page
        # while advertising 51. That must not be trusted as the whole feed.
        self.api.serve_full_feed = False
        main.start_run_budget()
        items, complete = main.fetch_feed()
        self.assertTrue(complete)
        self.assertEqual(len(items), CATALOG_SIZE)
        self.assertGreater(len(self.api.feed_requests()), len(main.FEED_NAMES),
                           "a short single response must fall back to paging")

    def test_fallback_is_refused_when_the_day_is_nearly_spent(self):
        # The fallback costs ~51 requests per feed. Spending it on 11 feeds 48
        # times a day would burn 26000 requests against a 1000/day quota, which
        # is exactly the overrun this whole design exists to prevent.
        self.api.serve_full_feed = False  # forces All to want the fallback
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps({
            "quota": {"date": main._utc_today(),
                      "used": main.DAILY_REQUEST_CEILING - 5},
        })
        main.start_run_budget()  # how a real run loads the day's prior spend
        items, complete = main.fetch_feed()

        self.assertFalse(complete, "a refused fallback means an incomplete read")
        self.assertIn("feed_fallback_skipped",
                      [e["kind"] for e in main._health_events])
        # The categories still answered, so their offers are kept; only All,
        # which needed the expensive retry, is missing.
        self.assertLess(len(items), CATALOG_SIZE)
        self.assertNotIn(("GET", f"{main.FEED_ENDPOINT}?page=2"), self.api.calls,
                         "the refused fallback must not page the feed anyway")

    def test_yesterdays_quota_does_not_count_against_today(self):
        self.store[main.HEALTH_STATE_FILENAME] = json.dumps({
            "quota": {"date": "2000-01-01", "used": 999999},
        })
        main.start_run_budget()
        self.assertEqual(main.load_quota_used(), 0)
        self.assertEqual(main.quota_remaining(), main.DAILY_REQUEST_CEILING)

    def test_quota_usage_accumulates_across_runs(self):
        self.api.serve_full_feed = True
        self.run_check()
        after_first = self.health_state()["quota"]["used"]
        self.assertGreater(after_first, 0)
        self.assertEqual(self.health_state()["quota"]["date"], main._utc_today())

        self.run_check()
        self.assertGreater(self.health_state()["quota"]["used"], after_first,
                           "each run must add its spend to the running total")

    def test_unreadable_quota_state_does_not_break_the_run(self):
        with mock.patch.object(main, "load_health_state",
                               side_effect=RuntimeError("store down")):
            self.assertEqual(main.load_quota_used(), 0)


class HealthAlertingTest(PipelineTestBase):
    """The failure-detection layer: it has to work when nothing else does."""

    def test_healthy_run_emits_an_ok_marker_and_stays_quiet(self):
        self.run_check()
        self.assertEqual(self.alerts, [], "a healthy run must not text the user")
        self.assertEqual(self.health_state()["last_status"], "ok")
        self.assertTrue(self.health_state().get("last_success"))

    def test_truncated_feed_is_reported_even_though_the_run_succeeds(self):
        """The original bug: a run that completes but silently read a fraction of the feed."""
        # Force the paginated path: the failure being reproduced is mid-pagination
        # truncation, and a single-request fetch has no page boundary to cut at.
        self.api.serve_full_feed = False
        main.start_run_budget()
        truncate_after = 13  # where the live service used to give up

        real_request = self.api.request

        def request(method, url, **kwargs):
            if "page=" in url and int(url.split("page=")[1]) > truncate_after:
                return FakeResponse(429, {"message": "Too Many Requests"})
            return real_request(method, url, **kwargs)

        with mock.patch.object(main.requests, "request", request):
            body, status = self.run_check()

        self.assertEqual(status, 200, "a partial read still did useful work")
        self.assertIn("health: degraded", body)
        kinds = {a[0] for a in self.alerts}
        self.assertTrue(self.alerts, "a truncated feed must reach the user")
        self.assertTrue(any("feed_incomplete" in k or "feed_too_small" in k for k in kinds),
                        f"expected a feed problem in the alert, got {kinds}")

    def test_unwritable_state_is_reported_because_it_causes_repeat_alerts(self):
        original = main.save_seen_deals
        with mock.patch.object(main, "save_seen_deals", lambda deals: False):
            body, status = self.run_check()
        self.assertIn("health: degraded", body)
        self.assertTrue(any("seen_state_unwritable" in a[0] for a in self.alerts))
        self.assertIs(main.save_seen_deals, original)

    def test_failed_notification_is_reported(self):
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            body, _ = self.run_check()
        self.assertIn("health: degraded", body)
        self.assertTrue(any("notification_failed" in a[0] for a in self.alerts))

    def test_repeat_failures_do_not_alert_every_run(self):
        """A persistent problem must not text the user hourly for days."""
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            self.run_check()
            first = len(self.alerts)
            for _ in range(5):
                self.run_check()
        self.assertEqual(first, 1)
        self.assertEqual(len(self.alerts), 1,
                         "the same ongoing problem must only alert once per cooldown")
        self.assertGreaterEqual(self.health_state()["consecutive_bad_runs"], 6)

    def test_cooldown_expiry_re_alerts(self):
        from datetime import datetime, timedelta, timezone
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            self.run_check()
            state = self.health_state()
            stale = datetime.now(timezone.utc) - timedelta(hours=main.ALERT_COOLDOWN_HOURS + 1)
            state["last_alert_sent"] = stale.isoformat()
            self.store[main.HEALTH_STATE_FILENAME] = json.dumps(state)
            self.run_check()
        self.assertEqual(len(self.alerts), 2)

    def test_a_different_problem_alerts_immediately(self):
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            self.run_check()
        with mock.patch.object(main, "save_seen_deals", lambda deals: False):
            self.run_check()
        self.assertEqual(len(self.alerts), 2, "a new kind of problem bypasses the cooldown")

    def test_recovery_is_announced_once(self):
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            self.run_check()
        self.alerts.clear()
        self.run_check()
        self.assertEqual([a[0] for a in self.alerts], ["recovered"])
        self.alerts.clear()
        self.run_check()
        self.assertEqual(self.alerts, [], "recovery is announced once, not every run")

    def test_tiny_feed_is_flagged_even_when_marked_complete(self):
        small = FakeResponse(200, {"Items": [make_item(1)], "TotalPages": 1})
        with mock.patch.object(main.requests, "request",
                               lambda method, url, **kw: small if "/feed/" in url
                               else self.api.request(method, url, **kw)):
            body, _ = self.run_check()
        self.assertIn("health: degraded", body)
        self.assertTrue(any("feed_too_small" in a[0] for a in self.alerts))

    def _serve_pages(self, pages):
        """Make the fake feed serve exactly `pages` complete pages."""
        def request(method, url, **kwargs):
            if "/feed/" in url:
                page = int(url.split("page=")[1]) if "page=" in url else 1
                if page > pages:
                    return FakeResponse(404, None, text='"NotFound"')
                start = (page - 1) * PAGE_SIZE
                return FakeResponse(200, {
                    "Items": [make_item(start + i) for i in range(PAGE_SIZE)],
                    "TotalPages": pages,
                })
            return self.api.request(method, url, **kwargs)
        return request

    def _establish_baseline(self):
        """Run enough healthy passes for the median baseline to become usable."""
        for _ in range(main.FEED_BASELINE_MIN_SAMPLES):
            self.run_check()
        self.alerts.clear()

    def test_baseline_needs_history_before_it_judges_anything(self):
        self.run_check()
        self.assertIsNone(main.feed_baseline(self.health_state()),
                          "one sample must not be treated as a baseline")
        self.assertEqual(self.alerts, [])

    def test_feed_shrinking_against_its_own_history_is_flagged(self):
        self._establish_baseline()
        sizes = self.health_state()["recent_feed_sizes"]
        self.assertEqual(main.feed_baseline(self.health_state()), CATALOG_SIZE)

        # 70 complete pages = 7000 items: above the 6000 floor but below 70% of
        # the 12000 median, so this isolates the ratio check from the floor.
        with mock.patch.object(main.requests, "request", self._serve_pages(70)):
            line = self.health_line()

        # feed_shrank is a notable event, so it reports in notes= rather than
        # problems=. It is a relative dip; feed_coverage_partial is the check
        # that pages when the shrink has a structural cause.
        self.assertIn("feed_shrank", line)
        self.assertIn("notes=", line)
        notes = line.split("notes=")[1].split(" ")[0]
        self.assertIn("feed_shrank", notes)
        self.assertNotIn("feed_too_small", line.split("problems=")[1].split(" ")[0])

    def test_a_truncated_run_never_moves_the_baseline(self):
        """A baseline fed by bad runs drifts down to meet the failure and disarms itself."""
        self._establish_baseline()
        before = self.health_state()["recent_feed_sizes"]

        with mock.patch.object(main.requests, "request", self._serve_pages(13)):
            self.run_check()

        self.assertEqual(self.health_state()["recent_feed_sizes"], before,
                         "an unhealthy run must not contribute to the baseline")

    def test_missing_total_pages_is_reported_as_a_schema_break(self):
        """Without TotalPages the loop reads one page and calls the feed complete."""
        def request(method, url, **kwargs):
            if "/feed/" in url:
                return FakeResponse(200, {"Items": [make_item(i) for i in range(PAGE_SIZE)]})
            return self.api.request(method, url, **kwargs)

        with mock.patch.object(main.requests, "request", request):
            body, _ = self.run_check()
        self.assertIn("health: degraded", body)
        self.assertTrue(any("feed_schema_invalid" in a[0] for a in self.alerts))

    def test_repeated_page_one_collapses_instead_of_inflating(self):
        """
        A feed that serves page one over and over used to inflate the item count
        to 5000 duplicates and sail past every size check. Merging by offer id
        makes that inflation structurally impossible: the same 100 offers now
        collapse to 100 distinct ones, which trips the size floor instead.
        """
        def request(method, url, **kwargs):
            if "/feed/" in url:
                return FakeResponse(200, {
                    "Items": [make_item(i) for i in range(PAGE_SIZE)],
                    "TotalPages": SERVED_PAGES,
                })
            return self.api.request(method, url, **kwargs)

        with mock.patch.object(main.requests, "request", request):
            body, _ = self.run_check()

        self.assertIn("feed_items=100", body.replace("Feed items: ", "feed_items="))
        self.assertIn("health: degraded", body)
        self.assertIn("feed_too_small", [e["kind"] for e in main._health_events])

    def test_feed_losing_its_title_text_is_flagged(self):
        """The matcher would silently match nothing while every count stayed green."""
        def request(method, url, **kwargs):
            if "/feed/" in url:
                page = int(url.split("page=")[1]) if "page=" in url else 1
                if page > SERVED_PAGES:
                    return FakeResponse(404, None, text='"NotFound"')
                start = (page - 1) * PAGE_SIZE
                items = []
                for i in range(PAGE_SIZE):
                    item = make_item(start + i)
                    item["Title"] = ""
                    items.append(item)
                return FakeResponse(200, {"Items": items, "TotalPages": SERVED_PAGES})
            return self.api.request(method, url, **kwargs)

        with mock.patch.object(main.requests, "request", request):
            body, _ = self.run_check()
        self.assertIn("health: degraded", body)
        self.assertTrue(any("feed_text_missing" in a[0] for a in self.alerts))

    def test_alerts_are_capped_per_incident(self):
        from datetime import datetime, timedelta, timezone
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            for i in range(main.MAX_REPEAT_ALERTS_PER_INCIDENT + 4):
                # Age the cooldown out each time so only the cap can stop it.
                state = self.health_state()
                if state.get("last_alert_sent"):
                    stale = datetime.now(timezone.utc) - timedelta(
                        hours=main.ALERT_COOLDOWN_HOURS + 1)
                    state["last_alert_sent"] = stale.isoformat()
                    state["alert_log"] = []
                    self.store[main.HEALTH_STATE_FILENAME] = json.dumps(state)
                self.run_check()

        self.assertLessEqual(len(self.alerts), main.MAX_REPEAT_ALERTS_PER_INCIDENT,
                             "one ongoing incident must not alert without bound")

    def test_alerts_are_capped_per_day(self):
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            for i in range(main.MAX_ALERTS_PER_DAY + 3):
                # Force a different signature each run so only the daily cap bites.
                state = self.health_state()
                state.pop("last_alert_signature", None)
                self.store[main.HEALTH_STATE_FILENAME] = json.dumps(state)
                self.run_check()
        self.assertLessEqual(len(self.alerts), main.MAX_ALERTS_PER_DAY)

    def test_canary_is_counted(self):
        self.run_check()
        state_line_hits = main.count_canary_hits([{"Title": "Refurbished Kindle"},
                                                  {"Title": "Widget"}])
        self.assertEqual(state_line_hits, 1)

    def test_health_marker_line_is_emitted_for_monitoring(self):
        with self.assertLogs(level="INFO") as logs:
            self.run_check()
        markers = [r for r in logs.output if main.HEALTH_MARKER in r and "status=" in r]
        self.assertTrue(markers, "every run must emit a machine-readable health line")
        self.assertIn("status=ok", markers[-1])

    def test_health_bookkeeping_never_breaks_the_actual_job(self):
        """If the health file itself is unreadable, deals must still go out."""
        def explode(*a, **k):
            raise RuntimeError("health store unavailable")

        with mock.patch.object(main, "load_health_state", explode):
            with mock.patch.object(main, "save_health_state", explode):
                try:
                    self.run_check()
                except Exception as e:
                    self.fail(f"health bookkeeping took down the run: {e}")
        self.assertEqual(len(self.sent), 1)


class KeywordCommandTest(PipelineTestBase):
    """
    The keyword list is edited by emailing the tracker's own inbox.

    That inbox is a personal Gmail account, so two things matter above all:
    only the owner can change the list, and no other mail is ever touched.
    """

    def stored_keywords(self):
        return json.loads(self.store[main.KEYWORDS_FILENAME])["keywords"]

    def reply_text(self):
        self.assertEqual(len(self.replies), 1, "expected exactly one reply")
        return self.replies[0].get_payload()

    def kinds(self):
        return [e["kind"] for e in main._health_events]

    # -- the list itself -----------------------------------------------------

    def test_first_run_seeds_the_list_from_the_defaults(self):
        self.run_check()
        self.assertEqual(self.stored_keywords(), main.DEFAULT_KEYWORDS)

    def test_the_stored_list_is_what_a_new_process_matches_with(self):
        self.store[main.KEYWORDS_FILENAME] = json.dumps({"keywords": ["lego"]})
        self.run_check()
        self.assertEqual(main._keywords, ["lego"])
        self.assertEqual(self.sent, [], "kindle is no longer on the list")

    def test_an_unreadable_list_fails_the_run_loudly(self):
        # Matching with the wrong list would mark offers seen unchecked, and a
        # seen offer never alerts -- so this must stop the run, not guess.
        self.store[main.KEYWORDS_FILENAME] = "{not json"
        body, status = self.run_check()
        self.assertEqual(status, 503)
        self.assertEqual(self.sent, [])
        self.assertTrue(any("keywords_unreadable" in a[0] for a in self.alerts))

    def test_an_empty_stored_list_counts_as_unreadable(self):
        self.store[main.KEYWORDS_FILENAME] = json.dumps({"keywords": []})
        _, status = self.run_check()
        self.assertEqual(status, 503)

    # -- applying commands ---------------------------------------------------

    def test_add_is_applied_saved_answered_and_marked_done(self):
        uid = self.gmail.add("woot add Lego, Switch 2")
        self.run_check()
        self.assertIn("lego", self.stored_keywords())
        self.assertIn("switch 2", self.stored_keywords())
        self.assertTrue(self.gmail.processed(uid))
        reply = self.replies[0]
        self.assertEqual(reply["To"], main.GMAIL_USER)
        self.assertIn("Added: lego", reply.get_payload())
        self.assertIsNone(main.parse_keyword_command(reply["Subject"]),
                          "a reply must never read back in as a command")

    def test_a_keyword_added_now_catches_new_offers_in_the_same_run(self):
        # First run: every offer is new. "Widget 1234" matches only the new keyword.
        self.gmail.add("woot add widget 1234")
        self.run_check()
        titles = [d["Title"] for d in self.sent[0]]
        self.assertIn("Widget 1234", titles)

    def test_remove_and_the_answers_for_unknown_and_duplicate_keywords(self):
        self.gmail.add("woot remove Kindle, nothing-like-this")
        self.gmail.add("woot add Air Tag")  # same as "air-tag" once hyphens flatten
        self.run_check()
        self.assertNotIn("kindle", self.stored_keywords())
        self.assertEqual(self.stored_keywords().count("air-tag"), 1)
        self.assertNotIn("air tag", self.stored_keywords())
        self.assertIn("Removed: kindle", self.replies[0].get_payload())
        self.assertIn('Not on the list: "nothing-like-this"', self.replies[0].get_payload())
        self.assertIn("Already on the list: air tag", self.replies[1].get_payload())

    def test_commands_apply_in_the_order_they_were_sent(self):
        self.gmail.add("woot add lego")
        self.gmail.add("woot remove lego")
        self.run_check()
        self.assertNotIn("lego", self.stored_keywords())

    def test_list_and_help_change_nothing_but_answer(self):
        self.gmail.add("woot list")
        self.run_check()
        self.assertEqual(self.stored_keywords(), main.DEFAULT_KEYWORDS)
        self.assertIn("kindle", self.reply_text())

    def test_short_broad_and_malformed_keywords_are_refused(self):
        self.gmail.add("woot add tv, widget, <script>")
        self.run_check()
        self.assertEqual(self.stored_keywords(), main.DEFAULT_KEYWORDS)
        reply = self.reply_text()
        self.assertIn('Not added "tv": it is too short', reply)
        # "widget" is in every fixture title: alerting on it would text every run.
        self.assertIn('Not added "widget": it matches', reply)
        self.assertIn('Not added "<script>": only letters', reply)

    def test_the_last_keyword_cannot_be_removed(self):
        self.store[main.KEYWORDS_FILENAME] = json.dumps({"keywords": ["lego"]})
        self.gmail.add("woot remove lego")
        self.run_check()
        self.assertEqual(self.stored_keywords(), ["lego"])
        self.assertIn("it is the last keyword", self.reply_text())

    def test_the_list_has_a_ceiling(self):
        full = [f"thing {i:02d}" for i in range(main.MAX_KEYWORDS)]
        self.store[main.KEYWORDS_FILENAME] = json.dumps({"keywords": full})
        self.gmail.add("woot add lego")
        self.run_check()
        self.assertNotIn("lego", self.stored_keywords())
        self.assertIn("the list is full", self.reply_text())

    # -- who may send commands -----------------------------------------------

    def test_gmail_dot_spelling_of_the_owner_still_counts_as_the_owner(self):
        # Gmail rewrites From on mail this account sends, adding the dots of
        # the account's display spelling. Seen on the real inbox, 2026-09-27.
        with mock.patch.object(main, "GMAIL_USER", "firstlast@gmail.com"):
            uid = self.gmail.add("woot add lego", sender="first.last@gmail.com")
            self.run_check()
        self.assertIn("lego", self.stored_keywords())
        self.assertTrue(self.gmail.processed(uid))

    def test_mail_forging_the_owners_address_is_ignored(self):
        # Same From as the owner, but it arrived from outside: no \Sent label.
        uid = self.gmail.add("woot remove kindle", sent=False)
        body, status = self.run_check()
        self.assertIn("kindle", self.stored_keywords())
        self.assertEqual(self.replies, [], "never answer unverified mail")
        self.assertTrue(self.gmail.processed(uid), "so it is not re-examined every run")
        self.assertIn("keyword_command_rejected", self.kinds())
        self.assertIn("health: ok", body, "an ignored forgery is not an outage")

    def test_another_allowed_address_needs_gmails_own_dmarc_pass(self):
        with mock.patch.object(main, "COMMAND_SENDERS", "Me@gmail.com"):
            self.gmail.add("woot add lego", sender="me@gmail.com", sent=False,
                           auth_results=[GMAIL_DMARC_PASS])
            self.run_check()
        self.assertIn("lego", self.stored_keywords())
        self.assertEqual(self.replies[0]["To"], "me@gmail.com")

    def test_a_forged_verdict_below_gmails_real_one_is_ignored(self):
        # Gmail prepends its own verdict; the sender's fake one sits underneath.
        with mock.patch.object(main, "COMMAND_SENDERS", "me@gmail.com"):
            self.gmail.add("woot add lego", sender="me@gmail.com", sent=False,
                           auth_results=["mx.google.com; dmarc=fail (p=NONE) header.from=gmail.com",
                                         GMAIL_DMARC_PASS])
            self.run_check()
        self.assertNotIn("lego", self.stored_keywords())
        self.assertEqual(self.replies, [])

    def test_a_verdict_for_a_lookalike_domain_does_not_count(self):
        verdict = GMAIL_DMARC_PASS.replace("header.from=gmail.com",
                                           "header.from=gmail.com.evil.example")
        with mock.patch.object(main, "COMMAND_SENDERS", "me@gmail.com"):
            self.gmail.add("woot add lego", sender="me@gmail.com", sent=False,
                           auth_results=[verdict])
            self.run_check()
        self.assertNotIn("lego", self.stored_keywords())

    def test_sender_chosen_text_inside_gmails_verdict_cannot_pass_it(self):
        # Gmail copies the envelope address, which the sender picks, into its own
        # header. Each of these is Gmail's genuine verdict of dmarc=fail.
        forged = {
            "envelope named dmarc=pass":
                "mx.google.com; spf=pass (google.com: domain of dmarc=pass@evil.example "
                "designates 192.0.2.1 as permitted sender) smtp.mailfrom=dmarc=pass@evil.example; "
                "dmarc=fail (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com",
            "quoted local part carrying a whole clause":
                'mx.google.com; spf=pass (google.com: domain of "x; dmarc=pass header.from=gmail.com;"'
                '@evil.example designates 192.0.2.1 as permitted sender) '
                'smtp.mailfrom="x; dmarc=pass header.from=gmail.com;"@evil.example; '
                "dmarc=fail (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com",
            "encoded word that decodes to a clause":
                "mx.google.com; spf=pass smtp.mailfrom==?utf-8?q?x=3B_dmarc=3Dpass_header.from"
                "=3Dgmail.com?=@evil.example; dmarc=fail (p=NONE) header.from=gmail.com",
            "no dmarc clause at all, pass text only in a comment":
                "mx.google.com; spf=pass (dmarc=pass header.from=gmail.com) "
                "smtp.mailfrom=x@evil.example",
        }
        for name, verdict in forged.items():
            with self.subTest(forgery=name):
                self.assertFalse(main._gmail_dmarc_pass(
                    f"Authentication-Results: {verdict}\r\n\r\n".encode(), "me@gmail.com"))
        genuine = f"Authentication-Results: {GMAIL_DMARC_PASS}\r\n\r\n".encode()
        self.assertTrue(main._gmail_dmarc_pass(genuine, "me@gmail.com"))

    def test_a_strangers_address_cannot_forge_a_monitoring_line(self):
        # The paging metric counts "WOOT_HEALTH status=failed" lines. A quoted
        # local part may contain exactly that text.
        self.gmail.add("woot list", sender='"WOOT_HEALTH status=failed"@evil.example', sent=False)
        with self.assertLogs(level="INFO") as logs:
            self.run_check()
        self.assertIn("keyword_command_rejected", self.kinds())
        self.assertFalse(any("WOOT_HEALTH status=failed" in line for line in logs.output))

    def test_a_stranger_is_ignored_even_with_a_genuine_verdict(self):
        self.gmail.add("woot remove kindle", sender="stranger@gmail.com", sent=False,
                       auth_results=[GMAIL_DMARC_PASS])
        self.run_check()
        self.assertIn("kindle", self.stored_keywords())
        self.assertEqual(self.replies, [])

    # -- leaving the rest of the inbox alone ---------------------------------

    def test_ordinary_mail_is_never_touched(self):
        uids = [self.gmail.add(s) for s in ("Woot Alert: 3 new deal(s) matching your keywords",
                                            "Re: woot add lego",
                                            "Fwd: woot list",
                                            "wooten add lego",
                                            "Woot keywords")]
        self.run_check()
        self.assertFalse(any(self.gmail.processed(u) for u in uids))
        self.assertEqual(self.replies, [])
        self.assertEqual(self.stored_keywords(), main.DEFAULT_KEYWORDS)

    def test_a_burst_of_commands_is_spread_across_runs(self):
        uids = [self.gmail.add(f"woot add thing {i:02d}")
                for i in range(main.MAX_COMMANDS_PER_RUN + 3)]
        self.run_check()
        self.assertEqual(sum(self.gmail.processed(u) for u in uids), main.MAX_COMMANDS_PER_RUN)
        self.run_check()
        self.assertTrue(all(self.gmail.processed(u) for u in uids))
        self.assertIn("thing 12", self.stored_keywords())

    def test_forged_mail_cannot_crowd_out_the_owners_command(self):
        for _ in range(main.MAX_COMMANDS_PER_RUN + 5):
            self.gmail.add("woot remove kindle", sent=False)
        real = self.gmail.add("woot add lego")
        self.run_check()
        self.assertTrue(self.gmail.processed(real))
        self.assertIn("lego", self.stored_keywords())
        self.assertIn("kindle", self.stored_keywords())

    def test_a_keyword_naming_a_whole_category_is_refused(self):
        # Every fixture offer sits in the Electronics category. The word is in
        # no title, but the pre-filter reads Categories too, so it would flag
        # every new offer and text on each run.
        self.gmail.add("woot add electronics")
        self.run_check()
        self.assertNotIn("electronics", self.stored_keywords())
        self.assertIn('Not added "electronics": it matches', self.reply_text())

    def test_a_short_run_budget_leaves_commands_for_the_next_run(self):
        uid = self.gmail.add("woot add lego")
        main.start_run_budget()
        main._run_deadline = main.time.monotonic() + main.COMMANDS_BUDGET_RESERVE - 1
        self.addCleanup(main.end_run_budget)
        self.assertEqual(main.process_keyword_commands([]), 0)
        self.assertFalse(self.gmail.processed(uid))

    def test_gmail_connections_verify_the_server_certificate(self):
        # Python 3.9's IMAP4_SSL and SMTP_SSL skip verification unless given a
        # context, and these connections carry the inbox's app password.
        self.gmail.add("woot list")
        self.run_check()
        for context in (self.gmail.connect_kwargs["ssl_context"], self.smtp_kwargs["context"]):
            self.assertEqual(context.verify_mode, main.ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
        self.assertTrue(self.smtp_kwargs["timeout"], "a hung reply must not stall the run")

    # -- failures must not cost deals ----------------------------------------

    def test_an_unreachable_inbox_does_not_affect_deal_alerts(self):
        self.gmail.fail_login = True
        body, status = self.run_check()
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sent), 1, "deals still go out")
        self.assertIn("keyword_commands_failed", self.kinds())
        self.assertIn("health: ok", body, "notable, not paging")
        self.assertEqual(self.alerts, [])

    def test_a_change_that_cannot_be_saved_is_retried_next_run(self):
        self.store[main.KEYWORDS_FILENAME] = json.dumps({"keywords": ["kindle"]})
        uid = self.gmail.add("woot add lego")
        with mock.patch.object(main, "save_keywords", lambda keywords: False):
            self.run_check()
        self.assertFalse(self.gmail.processed(uid), "left in place for the next run")
        self.assertEqual(self.stored_keywords(), ["kindle"])
        self.assertEqual(len(self.sent), 1, "deals still go out")

        self.run_check()
        self.assertEqual(self.stored_keywords(), ["kindle", "lego"])
        self.assertTrue(self.gmail.processed(uid))

    # -- parsing -------------------------------------------------------------

    def test_command_parsing(self):
        cases = {
            "woot add lego": ("add", ["lego"]),
            "WOOT ADD: Lego; Switch 2 ,": ("add", ["Lego", "Switch 2"]),
            "  woot delete kindle": ("remove", ["kindle"]),
            "woot list": ("list", []),
            "Woot Alert: 1 new deal(s)": None,
            "Re: woot add lego": None,
            "woot addition": None,
            "": None,
        }
        for subject, expected in cases.items():
            with self.subTest(subject=subject):
                self.assertEqual(main.parse_keyword_command(subject), expected)

    def test_keyword_cleaning(self):
        self.assertEqual(main.clean_keyword('  "Mac   Mini" '), ("mac mini", None))
        self.assertIsNotNone(main.clean_keyword("x" * (main.KEYWORD_MAX_CHARS + 1))[1])
        self.assertIsNotNone(main.clean_keyword("a-b")[1], "two letters is too short")
        self.assertIsNotNone(main.clean_keyword("lego\x1b[31m")[1])


def offer(title, **fields):
    """A detailed offer as getoffers returns it."""
    return {"Id": "id-" + title.lower().replace(" ", "-"), "Title": title, **fields}


class JevTestBase(PipelineTestBase):
    """A fake TypeSafe API: Jev's score for a listing is looked up by its title."""

    KEY = "ts-test-secret-key"

    def setUp(self):
        super().setUp()
        self.jev_calls = []
        self.jev_scores = {}    # title -> wanted score, or {product: score}
        self.jev_failures = []  # queued replies/exceptions used before real answers
        for p in (mock.patch.object(main, "TYPESAFE_API_KEY", self.KEY),
                  mock.patch.object(main.requests, "request", self._route)):
            p.start()
            self.addCleanup(p.stop)

    def _route(self, method, url, **kwargs):
        if url != main.JEV_ENDPOINT:
            return self.api.request(method, url, **kwargs)
        self.jev_calls.append(kwargs)
        if self.jev_failures:
            failure = self.jev_failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        wanted = self.jev_scores.get(kwargs["json"]["state"]["listing"]["title"], 0.99)
        answers = {}
        for qid in kwargs["json"]["questions"]:
            kind, product = qid.split("__")
            w = wanted.get(product, 0.99) if isinstance(wanted, dict) else wanted
            answers[qid] = {"noul": w if kind == "wanted" else round(1 - w, 3)}
        return FakeResponse(200, {"answers": answers, "model": main.JEV_MODEL})

    def screen(self, *deals, feed_items=()):
        main.reset_health_events()
        return main.screen_matches(list(deals), list(feed_items))

    def titles(self, deals):
        return [d["Title"] for d in deals]


class JevScreeningTest(JevTestBase):
    """
    Jev sets aside accessories and look-alikes. The rule that matters most:
    a real deal is never lost. Every failure keeps the deal, and anything Jev
    does set aside is still emailed.
    """

    # -- the decision --------------------------------------------------------

    def test_an_accessory_is_set_aside_and_the_product_is_texted(self):
        self.jev_scores = {"Case for Kindle Paperwhite": 0.02}
        to_text, filtered = self.screen(offer("Kindle Paperwhite 16GB"),
                                        offer("Case for Kindle Paperwhite"))
        self.assertEqual(self.titles(to_text), ["Kindle Paperwhite 16GB"])
        self.assertEqual([(d["Title"], w) for d, w, _ in filtered],
                         [("Case for Kindle Paperwhite", 0.02)])

    def test_only_scores_below_the_threshold_are_set_aside(self):
        self.jev_scores = {"Kobo Clara": main.JEV_DROP_BELOW,
                           "Kobo Sleeve": main.JEV_DROP_BELOW - 0.01}
        to_text, filtered = self.screen(offer("Kobo Clara"), offer("Kobo Sleeve"))
        self.assertEqual(self.titles(to_text), ["Kobo Clara"])
        self.assertEqual(self.titles(d for d, _, _ in filtered), ["Kobo Sleeve"])

    def test_a_bundle_survives_if_any_product_it_matched_is_really_in_it(self):
        self.jev_scores = {"Kindle and AirTag Bundle": {"ereader": 0.05, "airtag": 0.95},
                           "Kindle and AirTag Sticker Set": {"ereader": 0.05, "airtag": 0.04}}
        to_text, filtered = self.screen(offer("Kindle and AirTag Bundle"),
                                        offer("Kindle and AirTag Sticker Set"))
        self.assertEqual(self.titles(to_text), ["Kindle and AirTag Bundle"])
        self.assertEqual(len(filtered), 1)

    def test_keywords_without_a_screen_are_never_screened(self):
        # Montessori deliberately, and "lego" because no screen was set up for it.
        main.set_keywords(main.DEFAULT_KEYWORDS + ["lego"])
        self.jev_scores = {t: 0.01 for t in ("Montessori Busy Board",
                                             "Montessori Kindle Holder",
                                             "LEGO Kindle Stand")}
        to_text, filtered = self.screen(offer("Montessori Busy Board"),
                                        offer("Montessori Kindle Holder"),
                                        offer("LEGO Kindle Stand"))
        self.assertEqual(len(to_text), 3)
        self.assertEqual(filtered, [])
        self.assertEqual(self.jev_calls, [], "nothing to ask Jev about")

    def test_every_default_keyword_except_montessori_is_screened(self):
        unscreened = [k for k in main.DEFAULT_KEYWORDS
                      if main.normalize_text(k) not in main.JEV_KEYWORD_PRODUCT]
        self.assertEqual(unscreened, ["montessori"])

    def test_an_email_typed_spelling_finds_its_description(self):
        main.set_keywords(["air tag"])  # how "woot add Air Tag" would store it
        self.jev_scores = {"Air Tag Keychain Holder": 0.01}
        _, filtered = self.screen(offer("Air Tag Keychain Holder"))
        self.assertEqual(len(filtered), 1)

    # -- failing open --------------------------------------------------------

    def test_no_key_means_no_screening(self):
        with mock.patch.object(main, "TYPESAFE_API_KEY", None):
            to_text, filtered = self.screen(offer("Case for Kindle"))
        self.assertEqual((len(to_text), filtered, self.jev_calls), (1, [], []))

    def test_every_kind_of_failure_keeps_the_deal(self):
        def answers(value):
            return FakeResponse(200, {"answers": {
                f"{q}__ereader": {"noul": value} for q in ("wanted", "accessory", "unrelated")}})

        failures = {
            "unauthorised": [FakeResponse(401, {"error": "bad key"})],
            "rejected request": [FakeResponse(422, {"error": "invalid"})],
            "timeout": [main.requests.Timeout("read timed out")] * main.JEV_MAX_ATTEMPTS,
            "connection": [main.requests.ConnectionError("dns")] * main.JEV_MAX_ATTEMPTS,
            "still overloaded": [FakeResponse(529, {"error": "overloaded"})] * main.JEV_MAX_ATTEMPTS,
            "not json": [FakeResponse(200, None, text="<html>")],
            "no answers": [FakeResponse(200, {"answers": {}})],
            "not a number": [answers("high")],
            "not a probability": [answers(1.5)],
            "NaN": [answers(float("nan"))],
        }
        for name, replies in failures.items():
            with self.subTest(failure=name):
                self.jev_scores = {"Case for Kindle": 0.01}  # would be set aside
                self.jev_failures = list(replies)
                to_text, filtered = self.screen(offer("Case for Kindle"))
                self.assertEqual(self.titles(to_text), ["Case for Kindle"])
                self.assertEqual(filtered, [])
                self.assertIn("jev_unavailable", [e["kind"] for e in main._health_events])
                self.assertFalse(main.event_pages("jev_unavailable"),
                                 "a Jev outage must not page anyone")

    def test_a_brief_overload_is_retried_and_then_judged(self):
        self.jev_scores = {"Case for Kindle": 0.01}
        self.jev_failures = [FakeResponse(529, {}), FakeResponse(429, {})]
        _, filtered = self.screen(offer("Case for Kindle"))
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(self.jev_calls), 3)

    def test_a_short_run_budget_skips_jev_rather_than_risk_the_run(self):
        main._run_deadline = main.time.monotonic() + 5
        self.addCleanup(main.end_run_budget)
        self.jev_scores = {"Case for Kindle": 0.01}
        to_text, _ = self.screen(offer("Case for Kindle"))
        self.assertEqual(len(to_text), 1)
        self.assertEqual(self.jev_calls, [])

    # -- what Jev is sent ----------------------------------------------------

    def test_the_request_matches_what_was_measured(self):
        deal = offer("Kindle Paperwhite", Subtitle=None,
                     Features="<ul><li>6.8in <b>glare-free</b> display</li></ul>",
                     WriteUpBody="x" * 5000)
        feed = [{"OfferId": deal["Id"], "Title": "Kindle Paperwhite",
                 "Categories": ["Electronics", "Electronics/Tablets"]}]
        with self.assertLogs(level="DEBUG") as logs:
            self.screen(deal, feed_items=feed)
        sent = self.jev_calls[0]
        listing = sent["json"]["state"]["listing"]
        self.assertEqual(listing["features"], "6.8in glare-free display")
        self.assertEqual(len(listing["writeup"]), main.JEV_DETAIL_CHARS)
        self.assertEqual(listing["categories"], ["Electronics", "Electronics/Tablets"])
        self.assertNotIn("subtitle", listing)
        self.assertEqual(sent["json"]["model"], "jev-1.13.0")
        self.assertEqual(set(sent["json"]["questions"]),
                         {"wanted__ereader", "accessory__ereader", "unrelated__ereader"})
        self.assertIn(main.JEV_ACCESSORY_EXAMPLES,
                      sent["json"]["questions"]["accessory__ereader"]["instructions"]["question"])
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {self.KEY}")
        self.assertTrue(sent["timeout"])
        self.assertFalse(any(self.KEY in line for line in logs.output),
                         "the API key must never be logged")

    # -- through a whole run -------------------------------------------------

    def test_a_set_aside_match_is_emailed_not_texted_and_recorded_as_seen(self):
        self.jev_scores = {"Kobo Clara HD": 0.03}
        body, _ = self.run_check()
        self.assertEqual(sorted(self.titles(self.sent[0])),
                         ["Kindle Paperwhite 16GB", "Refurb E-Reader Bundle"])
        self.assertEqual([d["Title"] for d, _, _ in self.filtered[0]], ["Kobo Clara HD"])
        seen = json.loads(self.store[main.SEEN_DEALS_FILENAME])["deals"]
        self.assertIn("offer-04300", seen)
        self.assertIn("health: ok", body)

    def test_if_the_email_fails_set_aside_matches_are_retried_too(self):
        self.jev_scores = {t: 0.01 for t in ("Kindle Paperwhite 16GB", "Kobo Clara HD",
                                             "Refurb E-Reader Bundle")}
        with mock.patch.object(main, "send_notifications", lambda deals, filtered=(): False):
            self.run_check()
        seen = json.loads(self.store[main.SEEN_DEALS_FILENAME])["deals"]
        self.assertNotIn("offer-04300", seen)


def described(keyword, screen=True, **overrides):
    """An answer Claude could give for `keyword`: six listings to keep, two look-alikes."""
    name = keyword.title()
    answer = {
        "screen": screen,
        "reading": f"Any {keyword} itself, new or refurbished, alone or in a bundle.",
        "wanted_product": f"A {keyword}: the product itself, new, refurbished or used, "
                          f"alone or in a bundle that includes it.",
        "accessory_examples": "a mounting bracket, cover or replacement part",
        "keep": [f"Acme {name} Deluxe", f"Refurbished {name}", f"{name} 2-Pack",
                 f"{name} Starter Bundle", f"Brand New {name} (2026 Model)",
                 f"Acme Pro Series {name} with Extended Warranty and Free Shipping"],
        "set_aside": [f"Mounting Bracket for {name}", f"Cover for {name}"],
    }
    if not screen:
        answer.update(wanted_product="", accessory_examples="", keep=[], set_aside=[])
    answer.update(overrides)
    return answer


class FakeClaude:
    """anthropic.Anthropic, as far as describing a keyword uses it."""

    def __init__(self):
        self.calls = []
        self.client_kwargs = []
        self.answers = {}  # keyword -> answer, an exception, or a stop_reason to stop with
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def __call__(self, **kwargs):
        self.client_kwargs.append(kwargs)
        return self

    def keywords(self):
        return [c["messages"][0]["content"].removeprefix("Keyword: ") for c in self.calls]

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        keyword = kwargs["messages"][0]["content"].removeprefix("Keyword: ")
        answer = self.answers.get(keyword) or described(keyword)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return SimpleNamespace(stop_reason=answer, content=[])
        return SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=json.dumps(answer))])


class AutoScreenTest(JevTestBase):
    """
    A keyword with no hand-tested description gets one from Claude when it is
    added. Jev scores Claude's own sample listings with it, and the screen is
    used only if every listing it should keep clears AUTO_SCREEN_MIN_KEPT. Any
    failure leaves the keyword unscreened, which texts every match as before.
    """

    CLAUDE_KEY = "sk-ant-test-secret-key"
    WIDGET = "widget 1234"  # matches exactly one feed offer, "Widget 1234"

    def setUp(self):
        super().setUp()
        self.claude = FakeClaude()
        for p in (mock.patch.object(main, "ANTHROPIC_API_KEY", self.CLAUDE_KEY),
                  mock.patch.object(main.anthropic, "Anthropic", self.claude)):
            p.start()
            self.addCleanup(p.stop)

    def stored_screens(self):
        return json.loads(self.store.get(main.KEYWORD_SCREENS_FILENAME,
                                         '{"screens": {}}'))["screens"]

    def store_list(self, *extra, screens=None):
        self.store[main.KEYWORDS_FILENAME] = json.dumps(
            {"keywords": main.DEFAULT_KEYWORDS + list(extra)})
        if screens is not None:
            self.store[main.KEYWORD_SCREENS_FILENAME] = json.dumps({"screens": screens})

    def screen_on(self, keyword, **overrides):
        entry = {"status": "on", "keyword": keyword, "reading": f"Any {keyword}.",
                 "wanted_product": f"A {keyword} itself.", "accessory_examples": "a cover",
                 "check": {"keep": {f"Acme {keyword}": 0.99}, "set_aside": {}},
                 "model": main.AUTO_SCREEN_MODEL, "jev_model": main.JEV_MODEL,
                 "at": "2026-10-01T00:00:00+00:00"}
        entry.update(overrides)
        return entry

    def payloads(self):
        return [r.get_payload() for r in self.replies]

    def texted(self):
        return [d["Title"] for batch in self.sent for d in batch]

    def set_aside(self):
        return [d["Title"] for batch in self.filtered for d, _, _ in batch]

    # -- setting a screen up -------------------------------------------------

    def test_an_added_keyword_is_described_checked_and_reported_in_the_reply(self):
        self.jev_scores = {"Mounting Bracket for Widget 1234": 0.01}
        uid = self.gmail.add(f"woot add {self.WIDGET}")
        self.run_check()
        self.assertEqual(self.claude.keywords(), [self.WIDGET])
        entry = self.stored_screens()[self.WIDGET]
        self.assertEqual(entry["status"], "on")
        self.assertEqual(entry["jev_model"], main.JEV_MODEL)
        self.assertEqual(len(entry["check"]["keep"]), 6)
        self.assertTrue(self.gmail.processed(uid))
        self.assertEqual(len(self.replies), 1, "the result rides in the add reply itself")
        reply = self.payloads()[0]
        for text in ("Added: widget 1234", 'Jev screen for "widget 1234": on',
                     "Reading: Any widget 1234 itself", "set aside 1 of 2 look-alikes",
                     "0.99  Widget 1234", 'send "woot unscreen widget 1234"'):
            self.assertIn(text, reply)
        self.assertRegex(reply, r"widget 1234\s+screened \(auto\)")
        self.assertRegex(reply, r"kindle\s+screened\n")
        self.assertRegex(reply, r"montessori\s+not screened")

    def test_the_new_screen_judges_matches_from_the_same_run_on(self):
        self.jev_scores = {"Widget 1234": 0.02}
        self.gmail.add(f"woot add {self.WIDGET}")
        self.run_check()
        self.assertIn("Widget 1234", self.set_aside())
        self.assertNotIn("Widget 1234", self.texted())
        product = main._auto_product_id(self.WIDGET)
        questions = [c["json"]["questions"] for c in self.jev_calls
                     if f"wanted__{product}" in c["json"]["questions"]][-1]
        self.assertIn("A widget 1234: the product itself",
                      questions[f"wanted__{product}"]["instructions"]["wanted_product"])
        self.assertIn("a mounting bracket, cover or replacement part",
                      questions[f"accessory__{product}"]["instructions"]["question"])
        self.assertIn("set aside  0.02  Widget 1234", self.payloads()[0])

    def test_a_description_that_fails_its_self_check_is_not_used(self):
        # 0.3 would still be texted, but it is too close to the line to trust.
        self.jev_scores = {"Refurbished Widget 1234": 0.3, "Widget 1234": 0.01}
        self.gmail.add(f"woot add {self.WIDGET}")
        self.run_check()
        entry = self.stored_screens()[self.WIDGET]
        self.assertEqual(entry["status"], "off")
        self.assertIn('"Refurbished Widget 1234"', entry["reason"])
        self.assertIn("Widget 1234", self.texted())
        self.assertIn('Jev screen for "widget 1234": off', self.payloads()[0])
        self.run_check()
        self.assertEqual(len(self.claude.calls), 1, "a failed check is final, not retried")

    def test_a_keyword_that_names_no_product_is_left_unscreened(self):
        self.claude.answers[self.WIDGET] = described(
            self.WIDGET, screen=False, reading="It names a style, not a product.")
        self.gmail.add(f"woot add {self.WIDGET}")
        self.run_check()
        self.assertEqual(self.stored_screens()[self.WIDGET]["status"], "off")
        self.assertIn("Why: It names a style, not a product.", self.payloads()[0])
        product = main._auto_product_id(self.WIDGET)
        self.assertFalse(any(f"wanted__{product}" in c["json"]["questions"]
                             for c in self.jev_calls))

    def test_claudes_answer_is_cleaned_before_it_is_trusted(self):
        self.claude.answers["robe"] = described("robe", keep=[
            "Plush Robe", "Plush Robe", "Spa\x07 Robe", "  ",
            "Hooded Robe " + "x" * 300, "Hotel Spa Kimono, Waffle Knit"],
            set_aside=["Plush Robe", "Wardrobe Cabinet"])
        answer = main._describe_keyword("robe")
        self.assertEqual(answer["keep"][:2], ["Plush Robe", "Spa Robe"])
        self.assertEqual(len(answer["keep"]), 4, "duplicates and blanks go")
        self.assertEqual(len(answer["keep"][2]), 160)
        # The product's own name tests the description even without the keyword.
        self.assertIn("Hotel Spa Kimono, Waffle Knit", answer["keep"])
        self.assertEqual(answer["set_aside"], ["Wardrobe Cabinet"])

        self.claude.answers["robe"] = described("robe", keep=["Plush Robe", "", "Plush Robe"])
        with self.assertRaises(ValueError):
            main._describe_keyword("robe")
        self.claude.answers["robe"] = "max_tokens"
        with self.assertRaises(RuntimeError):
            main._describe_keyword("robe")

    def test_the_claude_request(self):
        self.gmail.add(f"woot add {self.WIDGET}")
        with self.assertLogs(level="DEBUG") as logs:
            self.run_check()
        call = self.claude.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["output_config"], {
            "effort": main.AUTO_SCREEN_EFFORT,
            "format": {"type": "json_schema", "schema": main.AUTO_SCREEN_SCHEMA}})
        self.assertEqual((call["fallbacks"], call["betas"]),
                         ("default", ["server-side-fallback-2026-07-01"]))
        self.assertEqual(call["messages"], [{"role": "user", "content": "Keyword: widget 1234"}])
        self.assertNotIn("Kindle Paperwhite 16GB", json.dumps(call),
                         "text from Woot listings never reaches the prompt")
        self.assertEqual(self.claude.client_kwargs[0], {
            "api_key": self.CLAUDE_KEY, "timeout": main.AUTO_SCREEN_TIMEOUT, "max_retries": 0})
        self.assertFalse(any(self.CLAUDE_KEY in line for line in logs.output),
                         "the API key must never be logged")

    # -- failing open --------------------------------------------------------

    def test_a_failed_setup_texts_matches_and_is_retried_later(self):
        self.claude.answers[self.WIDGET] = RuntimeError("overloaded")
        self.jev_scores = {"Widget 1234": 0.01}
        self.gmail.add(f"woot add {self.WIDGET}")
        line = self.health_line()
        entry = self.stored_screens()[self.WIDGET]
        self.assertEqual((entry["status"], entry["attempts"]), ("error", 1))
        self.assertIn("Widget 1234", self.texted())
        self.assertIn("status=ok", line)
        self.assertIn("keyword_screen_failed", line)
        self.assertIn("not set up yet", self.payloads()[0])

        self.run_check()  # inside the two-hour wait
        self.assertEqual(len(self.claude.calls), 1)

        screens = self.stored_screens()
        screens[self.WIDGET]["at"] = (datetime.now(timezone.utc)
                                      - timedelta(hours=3)).isoformat()
        self.store[main.KEYWORD_SCREENS_FILENAME] = json.dumps({"screens": screens})
        del self.claude.answers[self.WIDGET]
        self.run_check()
        self.assertEqual(len(self.claude.calls), 2)
        self.assertEqual(self.stored_screens()[self.WIDGET]["status"], "on")
        self.assertEqual(self.replies[-1]["To"], main.GMAIL_USER)
        self.assertIn('Jev screen for "widget 1234": on', self.payloads()[-1])

    def test_retries_back_off_up_to_two_days(self):
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)

        def due(hours_ago, attempts):
            return main._retry_due({"status": "error", "attempts": attempts,
                                    "at": (now - timedelta(hours=hours_ago)).isoformat()}, now)

        self.assertEqual([due(1.9, 1), due(2, 1)], [False, True])
        self.assertEqual([due(3.9, 2), due(4, 2)], [False, True])
        self.assertEqual([due(47, 12), due(48, 12)], [False, True])

    def test_without_a_claude_key_nothing_is_described(self):
        with mock.patch.object(main, "ANTHROPIC_API_KEY", None):
            self.gmail.add(f"woot add {self.WIDGET}")
            self.run_check()
        self.assertEqual(self.claude.calls, [])
        reply = self.payloads()[0]
        self.assertIn('Jev screen for "widget 1234": none', reply)
        self.assertRegex(reply, r"widget 1234\s+not screened")

    def test_a_setup_with_no_time_during_the_commands_happens_at_the_end_of_the_run(self):
        real = main.set_up_screen

        def short_during_commands(keyword, feed_items, reserve):
            if reserve == main.COMMANDS_BUDGET_RESERVE:
                return None
            return real(keyword, feed_items, reserve)

        with mock.patch.object(main, "set_up_screen", short_during_commands):
            self.gmail.add(f"woot add {self.WIDGET}")
            self.run_check()
        self.assertEqual(len(self.replies), 2)
        self.assertIn("being set up; the result follows in a separate email", self.payloads()[0])
        self.assertIn('Jev screen for "widget 1234": on', self.payloads()[1])
        self.assertEqual(self.replies[1]["To"], main.GMAIL_USER)

    def test_no_budget_means_no_claude_call_and_nothing_stored(self):
        main._run_deadline = main.time.monotonic() + 60
        self.addCleanup(main.end_run_budget)
        self.assertIsNone(main.set_up_screen(self.WIDGET, [], reserve=main.AUTO_SCREEN_END_RESERVE))
        self.assertEqual(self.claude.calls, [])
        self.assertNotIn(main.KEYWORD_SCREENS_FILENAME, self.store)

    def test_an_unreadable_screen_file_is_noted_and_left_for_a_person(self):
        self.store[main.KEYWORD_SCREENS_FILENAME] = "{not json"
        self.jev_scores = {"Kobo Clara HD": 0.01}
        self.gmail.add(f"woot add {self.WIDGET}")
        line = self.health_line()
        self.assertIn("status=ok", line)
        self.assertIn("keyword_screens_unreadable", line)
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.store[main.KEYWORD_SCREENS_FILENAME], "{not json")
        self.assertIn("Kobo Clara HD", self.set_aside(), "hand-tested screens keep working")

    def test_a_screen_checked_on_another_jev_model_is_described_again(self):
        self.store_list(self.WIDGET, screens={
            self.WIDGET: self.screen_on(self.WIDGET, jev_model="jev-0.9.0")})
        self.jev_scores = {"Widget 1234": 0.01}
        self.run_check()
        self.assertIn("Widget 1234", self.texted(), "an unmeasured description decides nothing")
        self.assertEqual(self.claude.keywords(), [self.WIDGET])
        self.assertEqual(self.stored_screens()[self.WIDGET]["jev_model"], main.JEV_MODEL)

    # -- the list over time --------------------------------------------------

    def test_keywords_added_before_screens_existed_are_set_up_one_per_run(self):
        self.store_list(self.WIDGET, "widget 4321")
        self.run_check()
        self.assertEqual(self.claude.keywords(), [self.WIDGET])
        self.assertEqual(self.replies[0]["To"], main.GMAIL_USER)
        self.assertIn('Jev screen for "widget 1234": on', self.payloads()[0])
        self.run_check()
        self.run_check()
        self.assertEqual(self.claude.keywords(), [self.WIDGET, "widget 4321"],
                         "hand-tested keywords and montessori are never sent to Claude")
        self.assertEqual(len(self.replies), 2)

    def test_unscreen_switches_jev_off_for_auto_and_hand_tested_keywords(self):
        self.store_list(self.WIDGET, screens={self.WIDGET: self.screen_on(self.WIDGET)})
        self.jev_scores = {"Widget 1234": 0.01, "Kindle Paperwhite 16GB": 0.01}
        self.gmail.add(f"woot unscreen {self.WIDGET}, Kindle, lego")
        self.run_check()
        self.assertIn("Widget 1234", self.texted())
        self.assertIn("Kindle Paperwhite 16GB", self.texted())
        self.assertEqual(self.set_aside(), [])
        screens = self.stored_screens()
        self.assertEqual((screens[self.WIDGET]["status"], screens["kindle"]["status"]),
                         ("off", "off"))
        reply = self.payloads()[0]
        self.assertIn("Unscreened: widget 1234", reply)
        self.assertIn("Unscreened: kindle", reply)
        self.assertIn('Not on the list: "lego"', reply)
        self.assertRegex(reply, r"kindle\s+not screened")
        self.assertEqual(self.claude.calls, [])

    def test_removing_a_keyword_forgets_its_screen(self):
        self.store_list(self.WIDGET, screens={
            self.WIDGET: self.screen_on(self.WIDGET, status="off", reason="unscreened")})
        self.gmail.add(f"woot remove {self.WIDGET}")
        self.run_check()
        self.assertNotIn(self.WIDGET, self.stored_screens())
        self.gmail.add(f"woot add {self.WIDGET}")
        self.run_check()
        self.assertEqual(self.claude.keywords(), [self.WIDGET])
        self.assertEqual(self.stored_screens()[self.WIDGET]["status"], "on")

    def test_a_deal_matching_two_screened_keywords_survives_if_either_says_keep(self):
        main.set_keywords(main.DEFAULT_KEYWORDS + ["lego"])
        main.set_screens({"lego": self.screen_on("lego")})
        lego = main._auto_product_id("lego")
        self.jev_scores = {"LEGO Kindle Stand": {"ereader": 0.01, lego: 0.9},
                           "LEGO Kindle Sticker": {"ereader": 0.01, lego: 0.05}}
        to_text, filtered = self.screen(offer("LEGO Kindle Stand"), offer("LEGO Kindle Sticker"))
        self.assertEqual(self.titles(to_text), ["LEGO Kindle Stand"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(set(self.jev_calls[0]["json"]["questions"]),
                         {f"{q}__{p}" for q in ("wanted", "accessory", "unrelated")
                          for p in ("ereader", lego)})


class NotificationTest(unittest.TestCase):
    """send_notifications is exercised for real, with SMTP stubbed out."""

    def _smtp(self):
        server = mock.MagicMock()
        server.__enter__ = mock.Mock(return_value=server)
        server.__exit__ = mock.Mock(return_value=False)
        return server

    def _send(self, deals, filtered):
        server = self._smtp()
        with mock.patch.object(main.smtplib, "SMTP_SSL", return_value=server):
            ok = main.send_notifications(deals, filtered)
        return ok, [c.args[0] for c in server.send_message.call_args_list]

    def test_set_aside_matches_alone_send_an_email_and_no_text(self):
        case = {"Id": "c", "Title": "Case for Kindle", "Url": "https://woot.com/offers/case"}
        ok, sent = self._send([], [(case, 0.02, 0.97)])
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1, "email only; nothing reaches the phone")
        self.assertEqual(sent[0]["To"], main.GMAIL_USER)
        self.assertIn("filtered out", sent[0]["Subject"])
        self.assertIn("Case for Kindle", sent[0].get_payload()[0].get_payload())

    def test_set_aside_matches_ride_along_in_a_normal_alert(self):
        good = {"Id": "g", "Title": "Kindle Paperwhite", "Url": "https://woot.com/offers/pw",
                "Items": [{"SalePrice": 99.99, "ListPrice": 149.99}]}
        bad = {"Id": "b", "Title": "<b>Case</b> for Kindle", "Url": 'https://x/"><script>'}
        ok, sent = self._send([good], [(bad, 0.02, 0.97)])
        self.assertTrue(ok)
        sms, email = sent
        self.assertIn("(1) deals", sms.get_payload()[0].get_payload())
        text, html_part = (p.get_payload() for p in email.get_payload())
        self.assertIn("not texted", text)
        self.assertIn("&lt;b&gt;Case&lt;/b&gt; for Kindle", html_part)
        self.assertNotIn("<script>", html_part, "offer text must be escaped in HTML")

    def test_null_fields_format_without_crashing(self):
        """A present-but-null Title used to raise TypeError and kill the whole send."""
        title, html, sms = main.format_deal_notifications(
            {"Id": "x", "Title": None, "Url": None, "Items": ["not-a-dict"]})
        self.assertEqual(title, "No Title")
        self.assertIn("No Title", html)
        self.assertLessEqual(len(sms), 140)

    def test_price_range_and_garbage_shapes_do_not_crash(self):
        for deal in (
            {"Id": "a", "Title": "Kindle", "SalePrice": [{"Minimum": 79.99}]},
            {"Id": "b", "Title": "Kindle", "SalePrice": ["junk"]},
            {"Id": "c", "Title": "Kindle", "Items": None},
            {"Id": "d", "Title": "Kindle", "Items": []},
        ):
            with self.subTest(deal=deal["Id"]):
                main.format_deal_notifications(deal)

    def test_one_unformattable_deal_does_not_block_the_good_ones(self):
        """Defense in depth: an unforeseen shape must not defer every other deal."""
        deals = [
            {"Id": "poison", "Title": "Broken", "Url": "u"},
            {"Id": "good", "Title": "Kindle Paperwhite 16GB",
             "Url": "https://woot.com/offers/kindle",
             "Items": [{"SalePrice": 99.99, "ListPrice": 149.99}]},
        ]
        real_format = main.format_deal_notifications

        def flaky(deal):
            if deal["Id"] == "poison":
                raise ValueError("unexpected offer shape")
            return real_format(deal)

        server = self._smtp()
        with mock.patch.object(main, "format_deal_notifications", flaky):
            with mock.patch.object(main, "record_health_event") as noted:
                with mock.patch.object(main.smtplib, "SMTP_SSL", return_value=server):
                    self.assertTrue(main.send_notifications(deals),
                                    "the good deal must still be sent")

        self.assertTrue(any("format" in c.args[0] for c in noted.call_args_list),
                        "the skipped deal must be recorded as a health problem")
        body = server.send_message.call_args_list[1][0][0].get_payload()[0].get_payload()
        self.assertIn("Kindle", body)
        self.assertNotIn("Broken", body)

    def test_send_returns_false_when_no_deal_can_be_formatted(self):
        def always_raises(deal):
            raise ValueError("nope")

        with mock.patch.object(main, "format_deal_notifications", always_raises):
            with mock.patch.object(main, "record_health_event"):
                with mock.patch.object(main.smtplib, "SMTP_SSL", return_value=self._smtp()):
                    self.assertFalse(main.send_notifications([{"Id": "x", "Title": "t"}]))

    def test_null_text_fields_do_not_break_the_alert(self):
        deals = [{
            "Id": "offer-04200",
            "Title": "Kindle Paperwhite 16GB",
            "Url": "https://woot.com/offers/kindle",
            "WriteUpBody": None,
            "Subtitle": None,
            "Items": [{"SalePrice": 99.99, "ListPrice": 149.99}],
        }]
        server = mock.MagicMock()
        server.__enter__ = mock.Mock(return_value=server)
        server.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(main.smtplib, "SMTP_SSL", return_value=server):
            self.assertTrue(main.send_notifications(deals))

        self.assertEqual(server.send_message.call_count, 2)
        sms = server.send_message.call_args_list[0][0][0].get_payload()[0].get_payload()
        self.assertIn("kindle", sms)
        self.assertLessEqual(len(sms), 140)


class HealthTieringTest(PipelineTestBase):
    """
    Two tiers: paging events escalate the run and reach the user, notable ones
    only annotate it.

    Before this split every one of twenty-odd checks escalated the run, so a
    harmless one (the seen index passing an ageing size ceiling) reported a
    problem every thirty minutes for a day. The danger of noise is not the
    annoyance: a real failure arriving in the middle of it is invisible.
    """

    def _report(self, kinds, status="ok"):
        """Record the given event kinds and emit one health summary line."""
        main.reset_health_events()
        for kind in kinds:
            main.record_health_event(kind, "synthetic")
        with self.assertLogs(level="INFO") as captured:
            reported = main.report_run_health(status, {"feed_items": 12000,
                                                       "feed_complete": "true"})
        lines = [r.getMessage() for r in captured.records
                 if r.getMessage().startswith(main.HEALTH_MARKER + " ")]
        return reported, lines[-1]

    def test_a_notable_event_keeps_the_run_ok(self):
        # The regression this guards against is worse than the noise it removed.
        # The absence policy fires when no "status=ok" line appears for three
        # hours, so if a notable event suppressed that string, demoting an event
        # that fires every run would trade a harmless alert for "the tracker has
        # not completed a healthy run" -- which means the service is dead.
        status, line = self._report(["feed_newly_capped"])
        self.assertEqual(status, "ok")
        self.assertIn("status=ok", line)
        self.assertEqual(self.alerts, [], "a notable event must not alert")

    def test_a_notable_event_is_still_visible_in_the_line(self):
        # Demoted, not hidden: it has to stay greppable for whoever is looking.
        _, line = self._report(["feed_newly_capped", "run_near_deadline"])
        notes = line.split("notes=")[1].split(" ")[0]
        self.assertEqual(sorted(notes.split(",")),
                         ["feed_newly_capped", "run_near_deadline"])
        self.assertIn("problems=none", line)

    def test_a_paging_event_still_escalates_and_alerts(self):
        status, line = self._report(["feed_empty"])
        self.assertEqual(status, "degraded")
        self.assertIn("problems=feed_empty", line)
        self.assertTrue(self.alerts, "a paging event must reach the user")

    def test_a_mixed_run_separates_the_two(self):
        _, line = self._report(["feed_empty", "feed_newly_capped"])
        self.assertIn("problems=feed_empty", line)
        self.assertIn("notes=feed_newly_capped", line)
        subject = self.alerts[0][0]
        self.assertIn("feed_empty", subject)
        self.assertNotIn("feed_newly_capped", subject)

    def test_an_unclassified_event_pages(self):
        # The default has to be loud. Forgetting to classify a new check should
        # over-alert, never silently disable it.
        status, _ = self._report(["some_check_added_next_year"])
        self.assertEqual(status, "degraded")

    def test_the_two_silent_failure_guards_are_never_notable(self):
        # Both describe the pipeline looking healthy while observing or matching
        # nothing -- the exact failure this service was written after -- and
        # canary_hits is only observed, never checked, so these are the only
        # guards against it. Demoting either is the mistake this test blocks.
        for kind in ("feed_not_changing", "feed_text_missing"):
            with self.subTest(kind=kind):
                self.assertNotIn(kind, main.NOTABLE_EVENTS)
                self.assertTrue(main.event_pages(kind))

    def test_a_notable_event_does_not_move_the_feed_baseline(self):
        # Notable events no longer escalate the status, so the baseline guard
        # can no longer key off status alone: a partial run must still not be
        # allowed to set the bar it will later be judged against.
        _, _ = self._report(["feed_shrank"])
        self.assertEqual(self.health_state().get("recent_feed_sizes", []), [])


class SeenStateChecksTest(PipelineTestBase):
    """The seen index: what is worth waking someone for, and what is not."""

    def test_a_truncated_index_still_pages(self):
        # The dangerous direction. An index that lost its contents re-notifies
        # every live deal on Woot, so this one keeps its alert.
        self.assertTrue(main.event_pages("seen_state_implausible"))

    def test_an_oversized_index_only_notes(self):
        # The harmless direction, and the one that actually fired for a day.
        self.assertIn("seen_state_oversized", main.NOTABLE_EVENTS)

    def test_the_ceiling_sits_above_a_plausible_steady_state(self):
        # The old 90000 was set when this service read one 5000-item feed; the
        # move to eleven tripled the growth rate and breached it. The index was
        # ~90800 on 2026-09-20 and peaks before a retention cohort expires, so
        # the backstop has to clear that by a wide margin to mean anything.
        self.assertGreater(main.SEEN_STATE_MAX, 200000)

    def _stale_index(self, count=600):
        old = (datetime.now(timezone.utc)
               - timedelta(days=main.SEEN_DEALS_RETENTION_DAYS + 5)).isoformat()
        return json.dumps(
            {"version": 1, "deals": {f"stale-{i}": old for i in range(count)}})

    def test_stale_entries_alone_report_nothing_because_pruning_works(self):
        # The check must not fire on merely having old entries: the prune runs
        # immediately before it and clears them. Otherwise it would be the same
        # kind of false alarm it replaced.
        self.store[main.SEEN_DEALS_FILENAME] = self._stale_index()
        main.reset_health_events()
        self.run_check()
        self.assertNotIn("seen_state_unpruned",
                         [e["kind"] for e in main._health_events])

    def test_a_broken_prune_is_reported(self):
        # The real failure: retention stops dropping anything and the file grows
        # without bound. Stating the invariant means this is caught directly
        # rather than inferred from a size that drifts with the catalogue.
        self.store[main.SEEN_DEALS_FILENAME] = self._stale_index()
        main.reset_health_events()
        with mock.patch.object(main, "prune_seen_deals", lambda deals: deals):
            self.run_check()
        self.assertIn("seen_state_unpruned",
                      [e["kind"] for e in main._health_events])

    def test_a_healthy_index_reports_neither(self):
        main.reset_health_events()
        self.run_check()
        kinds = [e["kind"] for e in main._health_events]
        for kind in ("seen_state_unpruned", "seen_state_oversized",
                     "seen_state_implausible"):
            self.assertNotIn(kind, kinds)


class FeedStalenessTest(unittest.TestCase):
    """
    The staleness check: the pipeline looking alive while observing nothing.

    Measured in elapsed hours because counting runs tied the threshold to the
    schedule. "24 runs" meant a day at the hourly cadence it was written for and
    silently became twelve hours when runs moved to every 30 minutes, which is
    what made it fire during an ordinary slow Sunday in September 2026. Before
    that this check had no test of its own, which is how the drift went unseen.
    """

    def setUp(self):
        main.reset_health_events()
        self.now = datetime(2026, 9, 20, 19, 0, tzinfo=timezone.utc)

    def _check(self, state, new_items=0, status="ok"):
        main.reset_health_events()
        return main._apply_staleness_check(
            status, ["none"], dict(state), {"new_items": new_items}, self.now)

    def _quiet_since(self, hours):
        return {"last_new_item_seen":
                (self.now - timedelta(hours=hours)).isoformat()}

    def test_a_normal_quiet_stretch_says_nothing(self):
        # 8-9h is the ordinary daily gap between Woot restocks, seen on every
        # one of sixteen consecutive days.
        status, _, _ = self._check(self._quiet_since(9))
        self.assertEqual(status, "ok")
        self.assertEqual(main._health_events, [])

    def test_the_slow_sunday_that_caused_this_no_longer_fires(self):
        # 2026-09-20: 14.5h without a new offer, the longest gap in sixteen days
        # and still ordinary -- Woot simply skipped an afternoon restock. Under
        # the old 24-RUN threshold that was 29 runs and alerted. Under 24 HOURS
        # it is quiet, which is what the rule always meant to say.
        status, _, _ = self._check(self._quiet_since(14.5))
        self.assertEqual(status, "ok")
        self.assertEqual(main._health_events, [])

    def test_a_full_day_without_new_offers_is_flagged(self):
        status, kinds, _ = self._check(self._quiet_since(25))
        self.assertEqual(status, "degraded")
        self.assertIn("feed_not_changing", kinds)
        self.assertEqual([e["kind"] for e in main._health_events],
                         ["feed_not_changing"])

    def test_the_boundary_is_the_configured_hours(self):
        under = self._check(self._quiet_since(main.NO_NEW_ITEMS_HOURS - 0.1))[0]
        over = self._check(self._quiet_since(main.NO_NEW_ITEMS_HOURS + 0.1))[0]
        self.assertEqual(under, "ok")
        self.assertEqual(over, "degraded")

    def test_new_offers_restart_the_clock(self):
        _, _, state = self._check(self._quiet_since(30), new_items=5)
        self.assertEqual(state["last_new_item_seen"], self.now.isoformat())
        self.assertEqual(main._health_events, [])

    def test_no_recorded_timestamp_starts_the_clock_instead_of_alerting(self):
        # A fresh state file must not alert about a gap it cannot measure.
        status, _, state = self._check({})
        self.assertEqual(status, "ok")
        self.assertEqual(state["last_new_item_seen"], self.now.isoformat())
        self.assertEqual(main._health_events, [])

    def test_a_corrupt_timestamp_starts_the_clock_instead_of_alerting(self):
        status, _, state = self._check({"last_new_item_seen": "not a date"})
        self.assertEqual(status, "ok")
        self.assertEqual(state["last_new_item_seen"], self.now.isoformat())

    def test_the_retired_run_counter_is_dropped(self):
        _, _, state = self._check(
            dict(self._quiet_since(1), consecutive_no_new_runs=24))
        self.assertNotIn("consecutive_no_new_runs", state)

    def test_an_already_failing_run_is_left_alone(self):
        # A run that already has a problem has a better explanation than
        # "nothing new appeared". This is also why a persistently noisy check
        # suppresses this one: throughout the September 2026 flood every run was
        # degraded, so this check never ran at all.
        status, _, _ = self._check(self._quiet_since(99), status="degraded")
        self.assertEqual(status, "degraded")
        self.assertEqual(main._health_events, [])

    def test_the_threshold_clears_the_observed_maximum_gap(self):
        # Sixteen days of production history: ordinary daily gap 8-9h, longest
        # observed 14.5h. A threshold at or under that is a false-alarm
        # generator, which is exactly what 24 runs (12h) had become.
        self.assertGreater(main.NO_NEW_ITEMS_HOURS, 14.5)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
