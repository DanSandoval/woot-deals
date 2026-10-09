#!/usr/bin/env python3
"""
Live check of the Jev screens Claude writes for keywords added by email.

Calls the real Claude and TypeSafe APIs through main.set_up_screen and
main.screen_matches -- the production code path -- for three keywords that
were added by email, then checks each screen against listings labelled by
hand. It fails if a description does not pass its own self-check, if Claude
answers too slowly for the run budget, or if any wanted listing would be set
aside. Nothing is written to the bucket. Skipped without both API keys.
Three Claude calls and about 60 Jev calls, roughly $0.10 a run.

Run: python test_auto_screen_live.py
"""
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("WOOT_API_KEY", "test-key")
os.environ.setdefault("GMAIL_USER", "sender@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "app-password")
os.environ.setdefault("EMAIL_RECIPIENT", "5551234567@example.com")
os.environ.setdefault("BUCKET_NAME", "test-bucket")

import main  # noqa: E402

# Woot-style titles, not the samples Claude writes for itself.
LABELLED = {
    "robe": {
        "wanted": ["Hotel Spa Unisex Waffle Robe", "Tommy Bahama Men's Terry Robe",
                   "Women's Long Fleece Robe with Hood, 2-Pack", "Kids Plush Animal Robe"],
        "not_wanted": ["Sauder Harbor View Wardrobe",
                       "Weber iGrill Meat Thermometer Probes, 2-Pack",
                       "Over-the-Door Robe Hook, Brushed Nickel", "Strobe Light Party Pack"],
    },
    "meta glasses": {
        "wanted": ["Ray-Ban Meta Glasses Gen 2 (Refurbished)", "Oakley Meta Glasses HSTN Sport",
                   "Meta Glasses Wayfarer Matte Black with Clear Lenses"],
        "not_wanted": ["Case for Meta Glasses", "Prescription Lens Inserts for Meta Glasses",
                       "Meta Glasses Charging Cable 2-Pack"],
    },
    "book nook": {
        "wanted": ["Rolife Book Nook Kit Sakura Densya",
                   "DIY Book Nook Kit: Mystery Detective Agency", "Book Nook Kit 3-Pack Bundle"],
        "not_wanted": ["Book Nook Dust Cover", "Book Nook LED Strip Light Kit"],
    },
}
# The screen exists to cut noise; on a sample this small, allow one miss in four.
MIN_NOT_WANTED_SET_ASIDE = 0.75


def user_env(name):
    value = os.environ.get(name)
    if not value and sys.platform == "win32":
        # A shell opened before the key was saved has not inherited it.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value = winreg.QueryValueEx(k, name)[0]
        except OSError:
            value = None
    return value


@unittest.skipUnless(user_env("TYPESAFE_API_KEY") and user_env("ANTHROPIC_API_KEY"),
                     "TYPESAFE_API_KEY and ANTHROPIC_API_KEY are both needed")
class AutoScreenLiveTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        describe = main._describe_keyword
        cls.seconds = {}

        def timed(keyword):
            start = time.monotonic()
            try:
                return describe(keyword)
            finally:
                cls.seconds[keyword] = time.monotonic() - start

        with mock.patch.object(main, "TYPESAFE_API_KEY", user_env("TYPESAFE_API_KEY")), \
                mock.patch.object(main, "ANTHROPIC_API_KEY", user_env("ANTHROPIC_API_KEY")), \
                mock.patch.object(main, "storage_client", None), \
                mock.patch.object(main, "_describe_keyword", timed):
            main.set_keywords(list(LABELLED))
            main.set_screens({})
            main.reset_health_events()
            cls.entries = {k: main.set_up_screen(k, [], reserve=0)[0] for k in LABELLED}
            deals = [{"Id": f"{k}-{label}-{i}", "Title": title}
                     for k, labels in LABELLED.items()
                     for label, titles in labels.items() for i, title in enumerate(titles)]
            _, filtered = main.screen_matches(deals, [])
            cls.events = list(main._health_events)
        cls.set_aside = {d["Title"]: w for d, w, _ in filtered}

        for keyword, entry in cls.entries.items():
            print(f"\n{keyword}: {entry['status']} in {cls.seconds.get(keyword, 0):.1f}s "
                  f"-- {entry.get('reading') or entry.get('reason')}")
            for kind, scores in (entry.get("check") or {}).items():
                for title, score in scores.items():
                    print(f"  {kind:9}  {score:.2f}  {title}")
            for label, titles in LABELLED[keyword].items():
                for title in titles:
                    verdict = (f"set aside {cls.set_aside[title]:.2f}"
                               if title in cls.set_aside else "texted")
                    print(f"  [{label}] {title}: {verdict}")

    def test_every_description_passes_its_self_check(self):
        failed = {k: e.get("reason") for k, e in self.entries.items() if e["status"] != "on"}
        self.assertEqual(failed, {})

    def test_claude_answers_well_inside_its_timeout(self):
        # A setup that times out is retried forever and never screens anything.
        slow = {k: round(s, 1) for k, s in self.seconds.items()
                if s > main.AUTO_SCREEN_TIMEOUT * 0.75}
        self.assertEqual(slow, {})

    def test_every_listing_was_actually_judged(self):
        self.assertEqual(self.events, [], "Jev failed on some listings; nothing was measured")

    def test_no_wanted_listing_is_set_aside(self):
        lost = [t for labels in LABELLED.values() for t in labels["wanted"] if t in self.set_aside]
        self.assertEqual(lost, [], "these real deals would not have been texted")

    def test_accessories_and_look_alikes_are_set_aside(self):
        noise = [t for labels in LABELLED.values() for t in labels["not_wanted"]]
        caught = [t for t in noise if t in self.set_aside]
        missed = [t for t in noise if t not in self.set_aside]
        self.assertGreaterEqual(len(caught) / len(noise), MIN_NOT_WANTED_SET_ASIDE,
                                f"only {len(caught)}/{len(noise)} set aside; missed {missed}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
