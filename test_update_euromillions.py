#!/usr/bin/env python3
"""Checks for update_euromillions.py. No network: the API listing and the
jackpot scrape are supplied by the tests, and every test runs in its own
empty directory.

    python3 -m unittest test_update_euromillions
"""
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import update_euromillions as feed

# Three consecutive draws, as held.
OLDER = dict(draw_id=4137, date="2026-09-29", numbers=(4, 7, 12, 31, 44), stars=(8, 11), jackpot=17000000)
LAST = dict(draw_id=4138, date="2026-10-02", numbers=(7, 8, 10, 22, 35), stars=(2, 10), jackpot=28871634)
NEW = dict(draw_id=4139, date="2026-10-06", numbers=(18, 22, 32, 41, 48), stars=(8, 10), jackpot=17000000)


def api_draw(draw_id, date, numbers, stars, jackpot):
    """A draw as the API lists it: numbers as strings, and the prize tiers the
    feed takes the draw's jackpot from."""
    return {
        "id": draw_id, "draw_id": draw_id * 10, "date": date, "has_winner": False,
        "numbers": [str(n) for n in numbers], "stars": [str(s) for s in stars],
        "prizes": [
            {"matched_numbers": 5, "matched_stars": 1, "prize": 199745.29, "winners": 4},
            {"matched_numbers": 5, "matched_stars": 2, "prize": jackpot, "winners": 0},
        ],
    }


def feed_draw(draw_id, date, numbers, stars, jackpot):
    """The same draw as the feed publishes it."""
    return {"id": draw_id, "date": date, "numbers": list(numbers), "stars": list(stars), "jackpot_eur": jackpot}


# The API lists oldest first; the feed publishes newest first.
API = [api_draw(**OLDER), api_draw(**LAST)]
PUBLISHED = [feed_draw(**LAST), feed_draw(**OLDER)]


def old_layout_feed(draws, jackpot=17000000, text="€17 Million Jackpot"):
    """The feed as it was published until October 2026: indented, and every
    draw carrying the API's whole payload as `raw`."""
    history = [dict(feed_draw(**d), raw=api_draw(**d)) for d in sorted(draws, key=lambda d: d["date"], reverse=True)]
    return json.dumps({
        "timestamp": "2026-10-06T18:12:00Z",
        "currentJackpotEUR": jackpot,
        "lastDraw": history[0],
        "history": history,
        "sources": {"api": feed.API_URL_DEFAULT, "jackpotPage": feed.JACKPOT_URL_DEFAULT,
                    "currentJackpotSource": "lottery.ie", "currentJackpotText": text},
    }, indent=2, ensure_ascii=False)


class FeedTest(unittest.TestCase):
    """Runs the script in an empty directory with the network replaced."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(directory.name)

    def run_feed(self, api, jackpot=(17000000, "€17 Million Jackpot"), at="2026-10-06T12:00:00", argv=()):
        """One run of the script at the UTC time `at`. `api` is the listing
        the API returns, or the error it fails with; `jackpot` is what the
        scrape returns, or the error it fails with. Returns the exit code."""
        class Clock(datetime.datetime):
            @classmethod
            def utcnow(cls):
                return cls.fromisoformat(at)

        def source(result):
            return mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result)

        self.scrape = source(jackpot)
        with mock.patch.object(feed, "fetch_json_with_retry", source(api)), \
             mock.patch.object(feed, "scrape_current_jackpot", self.scrape), \
             mock.patch.object(feed, "datetime", Clock), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return feed.main(list(argv))

    def put(self, text, path="euromillions.json"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path="euromillions.json"):
        with open(path, "rb") as f:
            return f.read()

    def published(self):
        return json.loads(self.read())


class PublishedFeed(FeedTest):
    def test_a_run_publishes_the_jackpot_the_last_draw_and_the_history_newest_first(self):
        self.assertEqual(self.run_feed(API), 0)
        published = self.published()
        self.assertEqual(published["currentJackpotEUR"], 17000000)
        self.assertEqual(published["lastDraw"], feed_draw(**LAST))
        self.assertEqual(published["history"], PUBLISHED)

    def test_the_feed_is_compact_json(self):
        self.run_feed(API)
        text = self.read().decode("utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")))

    def test_a_feed_in_the_old_layout_is_rewritten_without_the_api_payloads(self):
        self.put(old_layout_feed([OLDER, LAST]))
        self.run_feed(API)
        self.assertEqual(self.published()["history"], PUBLISHED)
        self.assertEqual(self.published()["lastDraw"], feed_draw(**LAST))
        self.assertNotIn(b"\n", self.read())


class NothingChanged(FeedTest):
    def test_a_run_that_finds_the_same_data_leaves_the_feed_as_it_was(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        before = self.read()
        self.assertEqual(self.run_feed(API, at="2026-10-06T18:00:00"), 0)
        self.assertEqual(self.read(), before)

    def test_the_same_jackpot_worded_differently_is_not_a_change(self):
        self.run_feed(API, jackpot=(17000000, "€17 Million Jackpot *"), at="2026-10-06T12:00:00")
        before = self.read()
        self.run_feed(API, jackpot=(17000000, "€17 Million Jackpot"), at="2026-10-06T18:00:00")
        self.assertEqual(self.read(), before)

    def test_a_new_jackpot_is_published_with_the_time_of_its_run(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        self.run_feed(API, jackpot=(30000000, "€30 Million Jackpot"), at="2026-10-07T06:00:00")
        self.assertEqual(self.published()["currentJackpotEUR"], 30000000)
        self.assertEqual(self.published()["timestamp"], "2026-10-07T06:00:00Z")

    def test_a_new_draw_is_published_with_the_time_of_its_run(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        self.run_feed(API + [api_draw(**NEW)], at="2026-10-07T06:00:00")
        self.assertEqual(self.published()["history"], [feed_draw(**NEW)] + PUBLISHED)
        self.assertEqual(self.published()["lastDraw"], feed_draw(**NEW))
        self.assertEqual(self.published()["timestamp"], "2026-10-07T06:00:00Z")

    def test_api_down_with_the_jackpot_unchanged_leaves_the_feed_as_it_was(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        before = self.read()
        self.assertNotEqual(self.run_feed(RuntimeError("429 Client Error: Too Many Requests"), at="2026-10-06T18:00:00"), 0)
        self.assertEqual(self.read(), before)

    def test_skip_scrape_does_not_scrape_and_keeps_the_published_jackpot(self):
        self.run_feed(API, jackpot=(30000000, "€30 Million Jackpot"), at="2026-10-06T12:00:00")
        before = self.read()
        self.assertEqual(self.run_feed(API, argv=["--skip-scrape"], at="2026-10-06T18:00:00"), 0)
        self.scrape.assert_not_called()
        self.assertEqual(self.read(), before)

    def test_a_published_timestamp_that_is_not_text_is_not_reused(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        self.put(json.dumps(dict(self.published(), timestamp=5), ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(self.run_feed(API, at="2026-10-06T18:00:00"), 0)
        self.assertEqual(json.loads(self.read("latest.json"))["timestamp"], "2026-10-06T18:00:00Z")

    def test_latest_json_and_the_site_change_only_when_the_feed_does(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        before = self.read("latest.json"), self.read("site/index.html")
        self.run_feed(API, at="2026-10-06T18:00:00")
        self.assertEqual((self.read("latest.json"), self.read("site/index.html")), before)
        self.assertEqual(json.loads(before[0])["timestamp"], self.published()["timestamp"])


class NeverLessThanPublished(FeedTest):
    API_DOWN = RuntimeError("429 Client Error: Too Many Requests")
    SCRAPE_DOWN = RuntimeError("403 Client Error: Forbidden")

    def test_api_down_keeps_the_published_draws_and_publishes_the_new_jackpot(self):
        self.put(old_layout_feed([OLDER, LAST]))
        code = self.run_feed(self.API_DOWN, jackpot=(30000000, "€30 Million Jackpot"))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.published()["history"], PUBLISHED)
        self.assertEqual(self.published()["lastDraw"], feed_draw(**LAST))
        self.assertEqual(self.published()["currentJackpotEUR"], 30000000)

    def test_a_run_that_fetched_nothing_leaves_the_feed_as_it_was(self):
        # Even a feed in the old layout, which any other run would rewrite.
        self.put(old_layout_feed([OLDER, LAST]))
        before = self.read()
        self.assertNotEqual(self.run_feed(self.API_DOWN, jackpot=self.SCRAPE_DOWN), 0)
        self.assertEqual(self.read(), before)

    def test_api_down_with_no_feed_published_writes_none(self):
        self.assertNotEqual(self.run_feed(self.API_DOWN), 0)
        self.assertFalse(os.path.exists("euromillions.json"))

    def test_an_empty_listing_leaves_the_feed_as_it_was(self):
        self.run_feed(API)
        before = self.read()
        self.assertNotEqual(self.run_feed([], at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.read(), before)

    def test_any_error_while_reading_the_api_keeps_the_published_draws(self):
        self.run_feed(API)
        before = self.read()
        self.assertNotEqual(self.run_feed(TypeError("'NoneType' object is not iterable"), at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.read(), before)

    def test_a_clean_listing_as_long_as_the_history_is_the_history(self):
        # The API files Friday's draw under Saturday, then corrects itself.
        misdated = dict(api_draw(**LAST), date="2026-10-03")
        self.run_feed([api_draw(**OLDER), misdated])
        self.assertEqual(self.run_feed(API, at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.published()["history"], PUBLISHED)

    def test_a_listing_with_a_bad_row_in_it_drops_nothing_published(self):
        # As long as the history, but not clean: the day it garbles is kept as published.
        self.run_feed(API)
        garbled = dict(api_draw(**OLDER), stars=["8"])
        self.assertNotEqual(self.run_feed([garbled, api_draw(**LAST), api_draw(**NEW)], at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.published()["history"], [feed_draw(**NEW)] + PUBLISHED)

    def test_a_shorter_listing_adds_its_draws_and_drops_none(self):
        self.run_feed(API)
        self.assertEqual(self.run_feed([api_draw(**NEW)], at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.published()["history"], [feed_draw(**NEW)] + PUBLISHED)
        self.assertEqual(self.published()["lastDraw"], feed_draw(**NEW))

    def test_a_listed_row_that_is_not_a_whole_draw_replaces_nothing_and_adds_nothing(self):
        self.run_feed(API)
        one_ball_short = dict(api_draw(**LAST), numbers=["7", "8", "10", "22"])
        no_such_star = dict(api_draw(**NEW), stars=["8", "13"])
        code = self.run_feed([api_draw(**OLDER), one_ball_short, no_such_star], at="2026-10-07T06:00:00")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.published()["history"], PUBLISHED)

    def test_a_listed_row_with_a_ball_or_a_star_too_many_is_not_trimmed_to_fit(self):
        self.run_feed(API)
        a_ball_too_many = dict(api_draw(**NEW), numbers=["18", "22", "32", "41", "48", "7"])
        a_star_too_many = dict(api_draw(**NEW), date="2026-10-09", stars=["8", "10", "3"])
        five_good_of_six = dict(api_draw(**NEW), date="2026-10-13", numbers=["18.5", "22", "32", "41", "48", "7"])
        code = self.run_feed(API + [a_ball_too_many, a_star_too_many, five_good_of_six], at="2026-10-14T06:00:00")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.published()["history"], PUBLISHED)

    # The API renaming a field would look like this: every row listed, none usable.
    NO_WHOLE_DRAW = [{"id": 4138, "date": "2026-10-02", "balls": ["7", "8", "10", "22", "35"], "stars": ["2", "10"]}]

    def test_a_listing_with_no_whole_draw_in_it_keeps_the_published_draws(self):
        self.run_feed(API)
        self.assertNotEqual(self.run_feed(self.NO_WHOLE_DRAW, at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.published()["history"], PUBLISHED)

    def test_a_listing_with_no_whole_draw_in_it_is_nothing_fetched(self):
        self.put(old_layout_feed([OLDER, LAST]))
        before = self.read()
        self.assertNotEqual(self.run_feed(self.NO_WHOLE_DRAW, jackpot=self.SCRAPE_DOWN), 0)
        self.assertEqual(self.read(), before)

    def test_api_down_keeps_published_draws_written_as_strings_of_digits(self):
        # The API's own form, which every app version reads.
        as_strings = [dict(d, numbers=[str(n) for n in d["numbers"]], stars=[str(n) for n in d["stars"]]) for d in PUBLISHED]
        self.put(json.dumps({"timestamp": "2026-10-06T18:12:00Z", "currentJackpotEUR": 17000000,
                             "lastDraw": as_strings[0], "history": as_strings,
                             "sources": {"currentJackpotSource": "lottery.ie"}}))
        self.run_feed(self.API_DOWN, jackpot=(30000000, "€30 Million Jackpot"))
        self.assertEqual(self.published()["history"], PUBLISHED)
        self.assertEqual(self.published()["currentJackpotEUR"], 30000000)

    def test_a_published_feed_that_starts_with_a_byte_order_mark_is_still_read(self):
        self.run_feed(API)
        with_mark = b"\xef\xbb\xbf" + self.read()
        with open("euromillions.json", "wb") as f:
            f.write(with_mark)
        self.assertEqual(self.run_feed([api_draw(**NEW)], at="2026-10-07T06:00:00"), 0)
        self.assertEqual(self.published()["history"], [feed_draw(**NEW)] + PUBLISHED)

    def test_one_bad_row_does_not_cost_the_good_ones(self):
        self.run_feed(API)
        bad = dict(api_draw(**NEW), date="2026-10-09", stars=["8"])
        self.assertNotEqual(self.run_feed(API + [api_draw(**NEW), bad], at="2026-10-10T06:00:00"), 0)
        self.assertEqual(self.published()["history"], [feed_draw(**NEW)] + PUBLISHED)

    def test_a_write_that_dies_leaves_the_published_feed_whole(self):
        self.run_feed(API, at="2026-10-06T12:00:00")
        before = self.read()
        with mock.patch.object(feed.os, "replace", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                self.run_feed(API + [api_draw(**NEW)], at="2026-10-07T06:00:00")
        self.assertEqual(self.read(), before)

    def test_scrape_down_keeps_the_published_jackpot(self):
        self.run_feed(API, jackpot=(30000000, "€30 Million Jackpot"), at="2026-10-06T12:00:00")
        before = self.read()
        self.assertEqual(self.run_feed(API, jackpot=self.SCRAPE_DOWN, at="2026-10-06T18:00:00"), 0)
        self.assertEqual(self.read(), before)

    def test_a_jackpot_page_that_no_longer_says_the_jackpot_keeps_the_published_one(self):
        self.run_feed(API, jackpot=(30000000, "€30 Million Jackpot"), at="2026-10-06T12:00:00")
        before = self.read()
        self.assertEqual(self.run_feed(API, jackpot=(None, None), at="2026-10-06T18:00:00"), 0)
        self.assertEqual(self.read(), before)

    def test_scrape_down_on_the_run_that_brings_a_new_draw_keeps_the_published_jackpot_and_fails(self):
        # The jackpot on file may now be the one the new draw was played for,
        # and nothing in the feed says so: the run has to.
        self.run_feed(API, jackpot=(30000000, "€30 Million Jackpot"), at="2026-10-06T12:00:00")
        self.assertNotEqual(self.run_feed(API + [api_draw(**NEW)], jackpot=self.SCRAPE_DOWN, at="2026-10-07T06:00:00"), 0)
        published = self.published()
        self.assertEqual(published["lastDraw"], feed_draw(**NEW))
        self.assertEqual(published["currentJackpotEUR"], 30000000)
        self.assertEqual(published["sources"]["currentJackpotSource"], "lottery.ie")
        self.assertEqual(published["sources"]["currentJackpotText"], "€30 Million Jackpot")

    def test_scrape_down_with_no_feed_published_uses_the_last_draw_jackpot(self):
        self.assertEqual(self.run_feed(API, jackpot=self.SCRAPE_DOWN), 0)
        self.assertEqual(self.published()["currentJackpotEUR"], LAST["jackpot"])
        self.assertEqual(self.published()["sources"]["currentJackpotSource"], "api")

    def test_scrape_still_down_moves_that_stand_in_on_to_the_newest_draw(self):
        self.run_feed(API, jackpot=self.SCRAPE_DOWN, at="2026-10-06T12:00:00")
        self.run_feed(API + [api_draw(**NEW)], jackpot=self.SCRAPE_DOWN, at="2026-10-07T06:00:00")
        self.assertEqual(self.published()["currentJackpotEUR"], NEW["jackpot"])
        self.assertEqual(self.published()["sources"]["currentJackpotSource"], "api")

    def test_a_scrape_that_confirms_a_stand_in_is_recorded(self):
        self.run_feed(API, jackpot=self.SCRAPE_DOWN, at="2026-10-06T12:00:00")
        self.run_feed(API, jackpot=(LAST["jackpot"], "€28,871,634 Jackpot"), at="2026-10-06T18:00:00")
        self.assertEqual(self.published()["sources"]["currentJackpotSource"], "lottery.ie")
        self.run_feed(API + [api_draw(**NEW)], jackpot=self.SCRAPE_DOWN, at="2026-10-07T06:00:00")
        self.assertEqual(self.published()["currentJackpotEUR"], LAST["jackpot"])

    def test_a_published_jackpot_that_is_not_a_positive_whole_number_is_not_kept(self):
        for damaged in ("NaN", "-5", "0", "17000000.5", '"17 million"', "true", "null"):
            with self.subTest(damaged):
                text = json.dumps({"timestamp": "2026-10-06T18:12:00Z", "currentJackpotEUR": "?",
                                   "lastDraw": PUBLISHED[0], "history": PUBLISHED,
                                   "sources": {"currentJackpotSource": "lottery.ie"}})
                self.put(text.replace('"?"', damaged))
                self.assertEqual(self.run_feed(API, jackpot=self.SCRAPE_DOWN), 0)
                self.assertEqual(self.published()["currentJackpotEUR"], LAST["jackpot"])
                self.assertEqual(self.published()["sources"]["currentJackpotSource"], "api")

    def test_a_feed_that_cannot_be_read_is_replaced_by_a_good_run_which_fails_to_say_so(self):
        for unreadable in ("<html>not the feed</html>", "[]", '"feed"', ""):
            with self.subTest(unreadable):
                self.put(unreadable)
                self.assertNotEqual(self.run_feed(API), 0)
                self.assertEqual(self.published()["history"], PUBLISHED)


class Draws(unittest.TestCase):
    def test_fetched_draws_win_their_day_and_published_draws_are_kept_newest_first(self):
        misprint = dict(feed_draw(**LAST), numbers=[1, 2, 3, 4, 5])
        merged = feed.merge_history([misprint, feed_draw(**OLDER)], [feed_draw(**LAST), feed_draw(**NEW)])
        self.assertEqual(merged, [feed_draw(**NEW), feed_draw(**LAST), feed_draw(**OLDER)])

    def test_a_published_draw_loses_the_api_payload(self):
        self.assertEqual(feed.complete_draw(dict(feed_draw(**LAST), raw=api_draw(**LAST))), feed_draw(**LAST))

    def test_only_one_whole_draw_on_a_real_day_is_a_draw(self):
        draw = feed_draw(**LAST)
        self.assertEqual(feed.complete_draw(draw), draw)
        edges = dict(draw, numbers=[1, 2, 3, 49, 50], stars=[1, 12])
        self.assertEqual(feed.complete_draw(edges), edges)
        # The API writes each number as a string of digits; that is read, as the app reads it.
        as_listed = dict(draw, numbers=["7", "8", "10", "22", "35"], stars=["2", "10"])
        self.assertEqual(feed.complete_draw(as_listed), draw)
        not_a_draw = (
            {"numbers": [7, 8, 10, 22]}, {"numbers": [7, 8, 10, 22, 35, 41]}, {"numbers": [7, 7, 10, 22, 35]},
            {"numbers": [7, 7, 8, 10, 22, 35]},
            {"numbers": [0, 8, 10, 22, 35]}, {"numbers": [7, 8, 10, 22, 51]}, {"numbers": ["7", "8", "10", "22", "x"]},
            {"numbers": [7.0, 8, 10, 22, 35]}, {"numbers": ["7.0", "8", "10", "22", "35"]}, {"numbers": [" 7", "8", "10", "22", "35"]},
            {"numbers": ["٧", "8", "10", "22", "35"]}, {"numbers": "7 8 10 22 35"},
            {"numbers": [True, 8, 10, 22, 35]}, {"numbers": None},
            {"stars": [2]}, {"stars": [2, 10, 11]}, {"stars": [2, 2]}, {"stars": [0, 10]}, {"stars": [2, 13]},
            {"date": "unknown"}, {"date": "2026-02-30"}, {"date": "2026-10-2"}, {"date": 20261002}, {"date": None},
        )
        for change in not_a_draw:
            with self.subTest(change):
                self.assertIsNone(feed.complete_draw({**draw, **change}))
        self.assertIsNone(feed.complete_draw("2026-10-02"))


if __name__ == "__main__":
    unittest.main()
