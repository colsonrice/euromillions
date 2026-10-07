#!/usr/bin/env python3
"""
update_euromillions.py

Adds a scrape of the Irish National Lottery EuroMillions page to obtain the
CURRENT upcoming jackpot (e.g., "€40 Million Jackpot *"), while still using
Pedro Mealha's API for last draw + history.

Writes:
  * euromillions.json   (compact JSON: the feed the PowerPlayAI app downloads)
      {
        "timestamp": "...Z",                  # when the data below last changed
        "currentJackpotEUR": <scraped euros>,
        "lastDraw": {"id", "date", "numbers", "stars", "jackpot_eur"},
        "history": [... every draw in that shape, newest first ...],
        "sources": {
          "api": "<api url>",
          "jackpotPage": "https://www.lottery.ie/draw-games/euromillions",
          "currentJackpotSource": "lottery.ie" | "api",
          "currentJackpotText": "€40 Million Jackpot *"   # when scraped
        }
      }

  * latest.json
      {
        "timestamp": "...Z",
        "date": "<latest draw date>",
        "jackpot_eur": <latest draw jackpot from API>,
        "current_jackpot_eur": <scraped euros>,           # NEW
        "numbers": [...],
        "stars":   [...],
        "verified": true
      }

  * site/index.html
      - Shows Current Jackpot (scraped) prominently

Source API:
  https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc

Scrape target:
  https://www.lottery.ie/draw-games/euromillions

Three rules the app that reads euromillions.json depends on:

1. Every draw is one whole draw on a real day: five different numbers and two
   different stars. A row from the API that is anything else is not published,
   and not trimmed or repaired to fit.
2. A run never publishes fewer draws than were published before. The API
   answers 429 to many runs; then the published history stands and only the
   jackpot is updated. A listing that is shorter than the published history,
   or has a bad row in it, adds its whole draws and drops nothing. Only a
   clean listing at least as long as the history replaces it. If the scrape
   fails, the published jackpot stands.
3. A run that changes nothing but the clock leaves the file alone, so the
   workflow commits nothing and the app's conditional request is answered 304.
   `timestamp` is when the data last changed, not when the script last ran.

The run exits non-zero when the API could not be read or listed a bad row,
when the published file could not be read, or when a draw was added but the
jackpot could not be scraped. It publishes what it did fetch first. A run that
fetched nothing writes nothing.

latest.json and site/ are not published: the workflow commits only
euromillions.json.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

API_URL_DEFAULT = "https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc"
JACKPOT_URL_DEFAULT = "https://www.lottery.ie/draw-games/euromillions"

# The feed the app downloads. The workflow checks out the published copy
# before the script runs, so this is also where the last run's data is read.
FEED_FILE = "euromillions.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; EuroMillionsFetcher/1.3; +github-actions)",
    "Accept-Language": "en-IE,en;q=0.9",
}


def fetch_json_with_retry(url: str, retries: int = 3, backoff_sec: float = 2.0) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff_sec * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_text_with_retry(url: str, retries: int = 3, backoff_sec: float = 2.0) -> str:
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff_sec * attempt)
    raise RuntimeError(f"Failed to fetch HTML {url}: {last_err}")


def _parse_euro_to_int(val: Any) -> Optional[int]:
    """Coerce numbers or strings like '€26,800,624' or '26800624.3' to int euros."""
    if val is None:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(round(val))
    if isinstance(val, str):
        m = re.findall(r"\d+(?:[.,]\d+)?", val.replace(",", ""))
        if m:
            try:
                return int(round(float(m[0].replace(",", ""))))
            except Exception:
                return None
    return None


def _to_int_maybe(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return None


def _extract_jackpot_from_tiers(raw: Dict[str, Any]) -> Optional[int]:
    """
    Prefer the 5+2 prize tier; else the max prize across tiers.
    Handles shapes like:
      {"matched_numbers":5,"matched_stars":2,"prize":26800624.3,"winners":0}
    nested anywhere.
    """
    tiers: List[Dict[str, Any]] = []

    def recurse(o: Any) -> None:
        if isinstance(o, dict):
            has_prize = any(k in o for k in ("prize", "amount", "jackpot"))
            if has_prize and ("matched_numbers" in o or "matched_stars" in o):
                tiers.append(o)
            for v in o.values():
                recurse(v)
        elif isinstance(o, list):
            for item in o:
                recurse(item)

    recurse(raw)

    for t in tiers:
        mn = _to_int_maybe(t.get("matched_numbers"))
        ms = _to_int_maybe(t.get("matched_stars"))
        if mn == 5 and ms == 2:
            prize = _parse_euro_to_int(t.get("prize") or t.get("amount") or t.get("jackpot"))
            if prize is not None:
                return prize

    best = None
    for t in tiers:
        p = _parse_euro_to_int(t.get("prize") or t.get("amount") or t.get("jackpot"))
        if p is not None and (best is None or p > best):
            best = p
    return best


def extract_jackpot_eur(raw: Dict[str, Any]) -> Optional[int]:
    """From an API draw object, derive the draw jackpot."""
    for k in ("jackpot", "prize", "jackpot_eur"):
        v = raw.get(k)
        j = _parse_euro_to_int(v)
        if j is not None:
            return j
    return _extract_jackpot_from_tiers(raw)


def normalize_draw(raw: Dict[str, Any]) -> Dict[str, Any]:
    # Date
    date_val = raw.get("date") or raw.get("draw_date") or raw.get("drawDate")
    date_iso = None
    if isinstance(date_val, str):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_val)
        if m:
            date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        else:
            try:
                date_iso = datetime.fromisoformat(date_val.replace("Z", "+00:00")).date().isoformat()
            except Exception:
                date_iso = date_val
    elif date_val is not None:
        date_iso = str(date_val)

    # As the API lists them. complete_draw() decides whether they are a draw.
    numbers = raw.get("numbers") or raw.get("numbers_main")
    stars = raw.get("stars") or raw.get("lucky_stars")
    jackpot_eur = extract_jackpot_eur(raw)

    return {
        "id": raw.get("id") or raw.get("draw_id") or raw.get("drawId"),
        "date": date_iso or "unknown",
        "numbers": numbers,
        "stars": stars,
        "jackpot_eur": jackpot_eur,
    }


def _pick(values: Any, count: int, highest: int) -> Optional[List[int]]:
    """
    `count` different whole numbers, each from 1 to `highest`, or None. The API
    writes each number as a string of digits and the feed as an integer; both
    are read, as the app reads them. Nothing is repaired: one entry that is
    neither spoils the list.
    """
    if not isinstance(values, list) or len(values) != count:
        return None
    picked: List[int] = []
    for v in values:
        if isinstance(v, str) and v.isascii() and v.isdigit():
            v = int(v)
        if type(v) is not int or not 1 <= v <= highest:
            return None
        picked.append(v)
    return picked if len(set(picked)) == count else None


def complete_draw(row: Any) -> Optional[Dict[str, Any]]:
    """
    The row as the feed publishes it, or None unless it is one whole draw on a
    real day: five different numbers from 1-50 and two different stars from
    1-12. Anything else is a row we do not understand. It is dropped whole;
    trimming it to fit could publish a wrong result under a real date.

    Rows from the API and rows already published both pass through here, so a
    published row loses anything else it carried (until October 2026 that was
    `raw`, the API's whole payload for the draw).
    """
    if not isinstance(row, dict):
        return None
    day = row.get("date")
    try:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            return None
    except ValueError:
        return None
    numbers, stars = _pick(row.get("numbers"), 5, 50), _pick(row.get("stars"), 2, 12)
    if numbers is None or stars is None:
        return None
    return {
        "id": row.get("id"),
        "date": day,
        "numbers": numbers,
        "stars": stars,
        "jackpot_eur": row.get("jackpot_eur"),
    }


def merge_history(published: Any, fetched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One draw per day, newest first. Fetched draws win their day; published
    draws the API did not list are kept.
    """
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in (published if isinstance(published, list) else []) + fetched:
        draw = complete_draw(row)
        if draw:
            by_day[draw["date"]] = draw
    return sort_desc_by_date(list(by_day.values()))


def fetch_draws(url: str) -> Tuple[List[Dict[str, Any]], int]:
    """
    The whole draws the API lists, and how many of its rows were not one.
    Raises if the API cannot be read.
    """
    api_raw = fetch_json_with_retry(url, retries=3, backoff_sec=2.0)
    if not isinstance(api_raw, list) or not api_raw:
        raise RuntimeError("Unexpected API response; expected non-empty list.")
    draws = [complete_draw(normalize_draw(d)) if isinstance(d, dict) else None for d in api_raw]
    whole = [d for d in draws if d]
    return whole, len(draws) - len(whole)


def load_published(path: str = FEED_FILE) -> Optional[Dict[str, Any]]:
    """
    The feed as last published: {} if there is none, None if one is there but
    cannot be read. A leading byte order mark is read past, as the app does.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            feed = json.load(f)
    except (OSError, ValueError):
        return None
    return feed if isinstance(feed, dict) else None


def same_data(payload: Dict[str, Any], published: Dict[str, Any]) -> bool:
    """
    True when the two feeds differ only in what describes the run: `timestamp`,
    and `sources` apart from `currentJackpotSource`. That one says whether the
    jackpot was scraped or stands in for one, and the next run reads it back,
    so a change to it is a change.
    """
    def data(feed: Dict[str, Any]) -> Dict[str, Any]:
        sources = feed.get("sources")
        source = sources.get("currentJackpotSource") if isinstance(sources, dict) else None
        return dict({k: v for k, v in feed.items() if k not in ("timestamp", "sources")}, currentJackpotSource=source)
    return data(payload) == data(published)


def write_whole(path: str, text: str) -> None:
    """
    Replaces the file in one step. The workflow commits the feed even after a
    failed run, so a run that dies mid-write must leave the old file whole.
    """
    partial = f"{path}.partial"
    with open(partial, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(partial, path)


def sort_desc_by_date(draws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key_fn(d: Dict[str, Any]):
        s = d.get("date") or ""
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
        if m:
            try:
                return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                return (0, 0, 0)
        return (0, 0, 0)
    return sorted(draws, key=key_fn, reverse=True)


def _multiplier_for_unit(unit: Optional[str]) -> int:
    if not unit:
        return 1
    u = unit.strip().lower()
    if u in ("million", "m"):
        return 1_000_000
    if u in ("billion", "b"):
        return 1_000_000_000
    if u in ("thousand", "k"):
        return 1_000
    return 1


def parse_current_jackpot_from_html(html_text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Extract something like "€40 Million Jackpot *" (with optional spaces/asterisk/decimals).
    Returns (euros_int, matched_text) or (None, None).
    """
    # Collapse whitespace for easier matching
    text = re.sub(r"\s+", " ", html_text)

    patterns = [
        # €40 Million Jackpot *, €40.5 Million Jackpot *
        r"€\s*([\d]+(?:[.,]\d+)?)\s*(Million|Billion|Thousand|M|B|K)\s*Jackpot(?:\s*\*)?",
        # €40,000,000 Jackpot
        r"€\s*([\d][\d.,]*)\s*Jackpot(?:\s*\*)?",
        # Jackpot €40,000,000 (fallback)
        r"Jackpot\s*€\s*([\d][\d.,]*)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue

        raw_num = m.group(1)
        unit = m.group(2) if m.lastindex and m.lastindex >= 2 else None
        try:
            val = float(raw_num.replace(",", ""))
            euros = int(round(val * _multiplier_for_unit(unit)))
            # Basic sanity for EuroMillions (avoid false tiny matches)
            if euros >= 1_000_000:
                return euros, m.group(0).strip()
        except Exception:
            continue

    return None, None


def scrape_current_jackpot(url: str) -> Tuple[Optional[int], Optional[str]]:
    html_text = fetch_text_with_retry(url, retries=3, backoff_sec=2.0)
    return parse_current_jackpot_from_html(html_text)


def render_html(out_path: str, context: Dict[str, Any]) -> None:
    latest = context["latest"]
    hist = context["history"]
    hist_count = len(hist)
    current_jackpot_eur = context.get("currentJackpotEUR")

    def balls_html(nums: List[int]) -> str:
        return "".join(f'<span class="ball">{n}</span>' for n in nums)

    def stars_html_fn(nums: List[int]) -> str:
        return "".join(f'<span class="star">{n}</span>' for n in nums)

    def fmt_eur(v: Any) -> str:
        return f"€{int(v):,}" if isinstance(v, (int, float)) else "—"

    numbers_html = balls_html(latest.get("numbers", []))
    stars_html = stars_html_fn(latest.get("stars", []))

    rows: List[str] = []
    for d in hist[:200]:
        date_str = d.get("date", "")
        nums_str = " ".join(str(x) for x in d.get("numbers", []))
        stars_str = " ".join(str(x) for x in d.get("stars", []))
        jv = d.get("jackpot_eur")
        jackpot_str = f"{int(jv):,}" if isinstance(jv, (int, float)) else ""
        rows.append(
            "<tr>"
            f"<td>{date_str}</td>"
            f"<td>{nums_str}</td>"
            f"<td>{stars_str}</td>"
            f"<td>{jackpot_str}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rows)

    jackpot_source = context.get("currentJackpotSource", "api")
    jackpot_note = "Current Jackpot (next draw)" + (" — source: lottery.ie" if jackpot_source == "lottery.ie" else " — source: API")

    html_str = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EuroMillions Status</title>
<style>
  :root {{ --bg:#0b1220; --fg:#e8eefc; --muted:#9bb0d3; --card:#121a30; }}
  body {{ background: linear-gradient(180deg,#0b1220,#10182e); color: var(--fg); font: 16px/1.5 system-ui, -apple-system, Segoe UI, Roboto, Ubuntu; margin:0; }}
  .wrap {{ max-width: 900px; margin: 40px auto; padding: 0 16px; }}
  .card {{ background: var(--card); border-radius: 20px; padding: 24px; box-shadow: 0 10px 35px rgba(0,0,0,.35); }}
  h1 {{ margin: 0 0 6px; font-weight: 700; }}
  .muted {{ color: var(--muted); }}
  .stat {{ display:flex; align-items:baseline; gap: 8px; }}
  .jackpot {{ font-size: clamp(28px, 6vw, 48px); font-weight: 800; letter-spacing: .5px; }}
  .row {{ display:flex; flex-wrap: wrap; gap: 20px; align-items:center; margin: 18px 0; }}
  .ball, .star {{ display:inline-grid; place-items:center; width: 42px; height:42px; border-radius: 999px; font-weight: 700; }}
  .ball {{ background:#1f2a4a; }}
  .star {{ background:#36301f; }}
  table {{ width:100%; border-collapse: collapse; margin-top: 14px; }}
  th, td {{ padding: 10px 8px; border-bottom: 1px solid rgba(255,255,255,.08); text-align:left; font-size: 14px; }}
  th {{ color: var(--muted); font-weight:600; }}
  footer {{ margin-top: 28px; color: var(--muted); font-size: 13px; }}
  a {{ color: #99c2ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>EuroMillions — Status</h1>
      <div class="muted">Last updated: {html.escape(context["timestamp"])} UTC</div>

      <div class="row" style="margin-top:16px;">
        <div class="stat">
          <div class="muted">{jackpot_note}</div>
          <div class="jackpot">{fmt_eur(current_jackpot_eur)}</div>
        </div>
      </div>

      <div class="row">
        <div>
          <div class="muted">Last Draw Date</div>
          <div style="font-weight:700">{latest.get("date","")}</div>
        </div>
        <div>
          <div class="muted">Numbers</div>
          <div>{numbers_html}</div>
        </div>
        <div>
          <div class="muted">Lucky Stars</div>
          <div>{stars_html}</div>
        </div>
      </div>

      <hr style="border:none; border-top:1px solid rgba(255,255,255,.1); margin: 12px 0 6px" />
      <div class="muted" style="font-size:14px">
        Sources: <a href="{html.escape(context['api'])}">euromillions.api.pedromealha.dev</a>
        &nbsp;|&nbsp;
        <a href="{html.escape(context.get('jackpotPage',''))}">lottery.ie (jackpot)</a>
      </div>
    </div>

    <div class="card" style="margin-top:18px">
      <h2>History (latest first)</h2>
      <div class="muted">{hist_count} draws</div>
      <table>
        <thead><tr><th>Date</th><th>Numbers</th><th>Stars</th><th>Jackpot (€)</th></tr></thead>
        <tbody>
{rows_html}
        </tbody>
      </table>
      <footer>Showing up to 200 latest draws.</footer>
    </div>
  </div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_str)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API_URL_DEFAULT, help="EuroMillions API endpoint.")
    ap.add_argument("--jackpot-url", default=JACKPOT_URL_DEFAULT, help="Page to scrape current jackpot from.")
    ap.add_argument("--skip-scrape", action="store_true", help="Disable scraping; currentJackpotEUR keeps its published value, or falls back to the API if there is none.")
    ap.add_argument("--out-dir", default="site", help="Directory to write the static site into.")
    args = ap.parse_args(argv)

    # Anything added here fails the run, after it has published what it could.
    trouble: List[str] = []

    published = load_published()
    if published is None:
        trouble.append(f"{FEED_FILE} was on file but could not be read, so nothing in it could be kept.")
        published = {}
    published_history = merge_history(published.get("history"), [])

    # 1) Fetch & normalize from API. If that fails, the published history stands.
    fetched: List[Dict[str, Any]] = []
    clean_listing = False
    try:
        fetched, skipped = fetch_draws(args.api)
        clean_listing = not skipped
        if skipped:
            trouble.append(f"Rows the API listed that are not a whole draw, and were not published: {skipped}.")
    except Exception as e:
        trouble.append(f"{type(e).__name__}: {e}")
        if not isinstance(e, RuntimeError):
            traceback.print_exc()  # not the API being down: leave the trail

    # 2) Scrape current jackpot (next draw)
    scraped_eur: Optional[int] = None
    matched_text: Optional[str] = None
    if not args.skip_scrape:
        try:
            scraped_eur, matched_text = scrape_current_jackpot(args.jackpot_url)
        except Exception:
            # Silent fallback; we still produce output from API
            scraped_eur, matched_text = None, None

    if not fetched and scraped_eur is None:
        print(f"⛔ Nothing fetched; {FEED_FILE} stands as published. {' '.join(trouble)}")
        return 1

    # The API lists every draw, so a clean listing at least as long as the
    # published history is the history: what the API corrects, the feed
    # corrects. Any other listing only adds to what is published. It replaces
    # the days it lists and drops none.
    listed = merge_history([], fetched)
    if clean_listing and len(listed) >= len(published_history):
        history = listed
    else:
        history = merge_history(published_history, listed)
    if not history:
        print(f"⛔ No draws fetched and none published; not writing {FEED_FILE}. {' '.join(trouble)}")
        return 1
    latest = history[0]
    latest_draw_jackpot = latest.get("jackpot_eur")

    # Choose current jackpot: prefer scraped; else keep the published one, with
    # the published account of where it came from. The last draw's own jackpot
    # is what was played for then, not what is on offer now, so it stands in
    # only when there is no scraped jackpot to keep. A published stand-in is
    # not kept: it follows the newest draw, as it always has.
    published_jackpot = published.get("currentJackpotEUR")
    published_sources = published.get("sources") if isinstance(published.get("sources"), dict) else {}
    if (type(published_jackpot) is not int or published_jackpot <= 0
            or published_sources.get("currentJackpotSource") == "api"):
        published_jackpot = None
    if scraped_eur is not None:
        current_jackpot, current_src, current_text = scraped_eur, "lottery.ie", matched_text
    elif published_jackpot:
        current_jackpot = published_jackpot
        current_src = published_sources.get("currentJackpotSource")
        current_text = published_sources.get("currentJackpotText")
        if published_history and latest["date"] > published_history[0]["date"]:
            # Nothing in the feed can say the jackpot is behind the draws, so the run does.
            trouble.append(f"The jackpot could not be scraped on the run that added the {latest['date']} draw; "
                           f"the published €{published_jackpot:,} may be what that draw was played for.")
    else:
        current_jackpot, current_src, current_text = latest_draw_jackpot, "api", None

    now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    payload = {
        "timestamp": now_iso,
        "currentJackpotEUR": current_jackpot,
        "lastDraw": latest,
        "history": history,
        "sources": {
            "api": args.api,
            "jackpotPage": args.jackpot_url,
            "currentJackpotSource": current_src,
        },
    }
    if current_text:
        payload["sources"]["currentJackpotText"] = current_text

    # 3) Write JSONs. A run that found nothing new leaves the feed alone, so
    # the workflow has nothing to commit. latest.json and the site stay on the
    # runner (the workflow publishes only the feed); they carry the feed's
    # timestamp, so they would not change between its changes either.
    changed = not same_data(payload, published)
    if changed:
        write_whole(FEED_FILE, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    feed_time = now_iso
    if not changed and isinstance(published.get("timestamp"), str):
        feed_time = published["timestamp"]

    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": feed_time,
                "date": latest.get("date"),
                "jackpot_eur": latest_draw_jackpot,      # from API (last draw)
                "current_jackpot_eur": current_jackpot,  # from scrape (preferred)
                "numbers": latest.get("numbers", []),
                "stars": latest.get("stars", []),
                "verified": True,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    # 4) Write static site
    out_html = os.path.join(args.out_dir, "index.html")
    render_html(out_html, {
        "timestamp": feed_time,
        "latest": latest,
        "history": history,
        "api": args.api,
        "currentJackpotEUR": current_jackpot,
        "currentJackpotSource": current_src,
        "jackpotPage": args.jackpot_url,
    })

    if changed:
        print(f"✅ Wrote {FEED_FILE} ({len(history)} draws), latest.json and {out_html}")
    else:
        print(f"✅ Nothing changed since {feed_time}; {FEED_FILE} stands as published. Wrote latest.json and {out_html}")
    if scraped_eur is not None:
        print(f"ℹ️  Scraped jackpot: €{scraped_eur:,} from {args.jackpot_url} ({matched_text})")
    elif published_jackpot:
        print(f"⚠️  Scrape unavailable; kept the published jackpot of €{published_jackpot:,}.")
    else:
        print("⚠️  Using API fallback for currentJackpotEUR (scrape unavailable).")
    if not fetched:
        print(f"⚠️  No draws fetched; kept the {len(history)} published.")
    elif len(listed) < len(published_history):
        print(f"⚠️  The API listed {len(listed)} draws, fewer than the {len(published_history)} published; none was dropped.")

    # Whatever was fetched is published above. The run still fails when
    # something let it down, so the failure shows on the Actions page.
    for problem in trouble:
        print(f"⛔ {problem}")
    return 1 if trouble else 0


if __name__ == "__main__":
    raise SystemExit(main())
