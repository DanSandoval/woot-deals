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


class PipelineTestBase(unittest.TestCase):
    """Shared fakes: Woot API with its rate limit, Cloud Storage, and the alert channel."""

    def setUp(self):
        self.api = FakeWootApi()
        self.store = {}
        self.sent = []
        self.alerts = []

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
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _send(self, deals):
        self.sent.append(list(deals))
        return True

    def _alert(self, subject, body):
        self.alerts.append((subject, body))
        return True

    def run_check(self):
        """Run one pass; returns (body, http_status)."""
        main._last_request_time = 0.0
        return main.check_woot_deals(None)

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

    def test_mac_mini_and_3d_printer_keywords_do_not_match_unrelated_offers(self):
        for title in ["Apple MacBook Air 13-inch",
                      "Apple iMac 24-inch M3",
                      "HP LaserJet Pro Printer",
                      "Sony 3D Blu-ray Player",
                      "Compact Mini Fridge"]:
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
        with mock.patch.object(main, "send_notifications", lambda deals: False):
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
        with mock.patch.object(main, "send_notifications", lambda deals: False):
            body, _ = self.run_check()
        self.assertIn("health: degraded", body)
        self.assertTrue(any("notification_failed" in a[0] for a in self.alerts))

    def test_repeat_failures_do_not_alert_every_run(self):
        """A persistent problem must not text the user hourly for days."""
        with mock.patch.object(main, "send_notifications", lambda deals: False):
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
        with mock.patch.object(main, "send_notifications", lambda deals: False):
            self.run_check()
            state = self.health_state()
            stale = datetime.now(timezone.utc) - timedelta(hours=main.ALERT_COOLDOWN_HOURS + 1)
            state["last_alert_sent"] = stale.isoformat()
            self.store[main.HEALTH_STATE_FILENAME] = json.dumps(state)
            self.run_check()
        self.assertEqual(len(self.alerts), 2)

    def test_a_different_problem_alerts_immediately(self):
        with mock.patch.object(main, "send_notifications", lambda deals: False):
            self.run_check()
        with mock.patch.object(main, "save_seen_deals", lambda deals: False):
            self.run_check()
        self.assertEqual(len(self.alerts), 2, "a new kind of problem bypasses the cooldown")

    def test_recovery_is_announced_once(self):
        with mock.patch.object(main, "send_notifications", lambda deals: False):
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
            body, _ = self.run_check()

        self.assertIn("health: degraded", body)
        kinds = " ".join(a[0] for a in self.alerts)
        self.assertIn("feed_shrank", kinds)
        self.assertNotIn("feed_too_small", kinds)

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
        with mock.patch.object(main, "send_notifications", lambda deals: False):
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
        with mock.patch.object(main, "send_notifications", lambda deals: False):
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


class NotificationTest(unittest.TestCase):
    """send_notifications is exercised for real, with SMTP stubbed out."""

    def _smtp(self):
        server = mock.MagicMock()
        server.__enter__ = mock.Mock(return_value=server)
        server.__exit__ = mock.Mock(return_value=False)
        return server

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


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
