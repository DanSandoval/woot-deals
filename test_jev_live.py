#!/usr/bin/env python3
"""
Live check of the Jev screen against labelled Woot listings.

Calls the real TypeSafe API through main.screen_matches -- the production code
path, not a separate script -- so a model, prompt or threshold change that
starts setting aside real deals fails here before it ships. Skipped without a
TYPESAFE_API_KEY. One call per listing, about $0.004 a run.

testdata/jev_corpus.json holds 120 listings labelled on 2026-09-27: 49 live
Woot offers that matched the keywords that day, and 71 constructed hard cases
(accessories, look-alike words, awkward titles for real products).

Run: python test_jev_live.py
"""
import json
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("WOOT_API_KEY", "test-key")
os.environ.setdefault("GMAIL_USER", "sender@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "app-password")
os.environ.setdefault("EMAIL_RECIPIENT", "5551234567@example.com")
os.environ.setdefault("BUCKET_NAME", "test-bucket")

import main  # noqa: E402

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "jev_corpus.json")
NOT_WANTED = {"accessory", "false_substring", "unrelated"}
# The screen exists to cut noise; below this it has quietly stopped working.
MIN_NOT_WANTED_SET_ASIDE = 0.90


def typesafe_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key and sys.platform == "win32":
        # A shell opened before the key was saved has not inherited it.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0]
        except OSError:
            key = None
    return key


def as_offer(item):
    """The listing as getoffers returns it, plus its feed entry (for Categories)."""
    deal = {"Id": item["id"], "Title": item["title"], "Subtitle": item.get("subtitle"),
            "Features": item.get("features"), "WriteUpBody": item.get("writeup"),
            "Url": item.get("url")}
    feed = {"OfferId": item["id"], "Title": item["title"],
            "Categories": item.get("categories") or []}
    return deal, feed


@unittest.skipUnless(typesafe_key(), "TYPESAFE_API_KEY is not set")
class JevLiveTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        main.set_keywords(main.DEFAULT_KEYWORDS)
        main.reset_health_events()
        with open(CORPUS, encoding="utf-8") as f:
            corpus = json.load(f)

        # Only listings the keywords actually match ever reach the screen.
        cls.items = [i for i in corpus if main.is_matching_deal(as_offer(i)[0])]
        offers = [as_offer(i) for i in cls.items]
        with mock.patch.object(main, "TYPESAFE_API_KEY", typesafe_key()):
            to_text, filtered = main.screen_matches([d for d, _ in offers],
                                                    [f for _, f in offers])
        cls.texted = {d["Id"] for d in to_text}
        cls.set_aside = {d["Id"]: w for d, w, _ in filtered}
        cls.screened = {i["id"] for i in cls.items
                        if main._jev_products_for(as_offer(i)[0]) is not None}
        cls.events = list(main._health_events)

        print(f"\n{len(cls.items)} matching listings, {len(cls.screened)} screened, "
              f"{len(cls.set_aside)} set aside")
        for item in cls.items:
            if item["id"] in cls.set_aside:
                print(f"  set aside {cls.set_aside[item['id']]:.2f}  "
                      f"[{item['label']}] {item['title']}")

    def test_every_listing_was_actually_judged(self):
        # Failures keep deals, which would make the next test pass vacuously.
        self.assertEqual(self.events, [], "Jev failed on some listings; nothing was measured")

    def test_no_wanted_listing_is_set_aside(self):
        lost = [i["title"] for i in self.items
                if i["label"] == "wanted" and i["id"] in self.set_aside]
        self.assertEqual(lost, [], "these real deals would not have been texted")
        wanted = [i for i in self.items if i["label"] == "wanted"]
        self.assertTrue(wanted and all(i["id"] in self.texted for i in wanted))

    def test_accessories_and_look_alikes_are_set_aside(self):
        noise = [i for i in self.items
                 if i["label"] in NOT_WANTED and i["id"] in self.screened]
        caught = [i for i in noise if i["id"] in self.set_aside]
        missed = [i["title"] for i in noise if i["id"] not in self.set_aside]
        self.assertGreaterEqual(len(caught) / len(noise), MIN_NOT_WANTED_SET_ASIDE,
                                f"only {len(caught)}/{len(noise)} set aside; missed {missed}")

    def test_live_woot_listings_are_all_handled_correctly(self):
        live = [i for i in self.items if i["source"] == "live" and i["id"] in self.screened]
        wrong = [(i["label"], i["title"]) for i in live
                 if (i["id"] in self.set_aside) != (i["label"] in NOT_WANTED)]
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
