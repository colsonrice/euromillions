#!/usr/bin/env python3
"""
update_euromillions.py

- Scrapes Ireland NL EuroMillions history page for the latest draw
  (date, 5 numbers, 2 stars) and the displayed jackpot (€).
- Fetches full historical draws via Pedro Mealha's public API.
- Verifies the scraped latest draw against the latest API draw.
- Writes:
    * euromillions.json  (full payload with history + latest)
    * latest.json        (handy small object)
    * site/index.html    (a simple static status page)

Sources:
- Irish National Lottery EuroMillions history:
  https://www.lottery.ie/results/euromillions/history
- Euromillions API (v1/draws):
  https://euromillions.api.pedromealha.dev
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


LOTTERY_IE_HISTORY_URL = "https://www.lottery.ie/results/euromillions/history"
EUROM_API_URL = "https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; EuroMillionsFetcher/1.0; +github-actions)"
}


def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_json(url: str) -> Any:
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    return r.json()


def _parse_euro_to_int(euro_text: str) -> int:
    # "€26,800,624" -> 26800624
    digits = re.sub(r"[^\d]", "", euro_text)
    if not digits:
        raise ValueError(f"Could not parse euro amount: {euro_text!r}")
    return int(digits)


def _iso_from_ddmmyy(s: str) -> str:
    # Matches "26/08/25" → "2025-08-26"
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{2})\b", s)
    if not m:
        raise ValueError(f"Could not find dd/mm/yy in: {s[:120]!r}")
    d, mth, yy = map(int, m.groups())
    year = 2000 + yy  # EuroMillions started in 2004; 20xx is safe
    return date(year, mth, d).isoformat()


def scrape_latest_from_irish_history(html_text: str) -> Dict[str, Any]:
    """
    Returns:
      {
        "date": "YYYY-MM-DD",
        "numbers": [n1..n5],
        "stars": [s1, s2],
        "jackpot_eur": int
      }
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # 1) Find the first "Jackpot" label on page (the top/most recent card)
    jackpot_label = soup.find(string=re.compile(r"\bJackpot\b", re.I))
    if not jackpot_label:
        raise RuntimeError("Couldn't find 'Jackpot' label on the page.")
    # The amount should be very near; look for a euro string right after.
    jackpot_node = jackpot_label.find_next(string=re.compile(r"€[\s\d,]+"))
    if not jackpot_node:
        # Some builds wrap it in <p class="font-black text-xl">…</p>
        euro_p = jackpot_label.find_next("p", class_=re.compile(r"\bfont-black\b.*\btext-xl\b"))
        if euro_p:
            jackpot_text = euro_p.get_text(strip=True)
        else:
            raise RuntimeError("Couldn't find jackpot amount after 'Jackpot'.")
    else:
        jackpot_text = jackpot_node.strip()
    jackpot_eur = _parse_euro_to_int(jackpot_text)

    # 2) Constrain to the latest card container:
    section = jackpot_label
    for _ in range(4):
        if hasattr(section, "parent") and section.parent:
            section = section.parent
        else:
            break

    section_text = section.get_text(" ", strip=True)

    # 3) Date in header like "Tue 26/08/25"
    latest_date_iso = _iso_from_ddmmyy(section_text)

    # 4) Winning numbers: capture the first 5 integers after "Winning numbers".
    numbers_label = section.find(string=re.compile(r"Winning numbers", re.I))
    if not numbers_label:
        raise RuntimeError("Couldn't find 'Winning numbers' label.")
    numbers: List[int] = []
    for el in numbers_label.find_all_next(True, limit=60):
        t = el.get_text(strip=True)
        if re.fullmatch(r"\d{1,2}", t):
            numbers.append(int(t))
            if len(numbers) == 5:
                break
        # stop scanning if we hit the "Lucky Stars" label
        if isinstance(el.string, str) and re.search(r"Lucky\s+Stars", el.string, re.I):
            break
    if len(numbers) != 5:
        # Fallback: scan raw text between labels
        after = section_text.split("Winning numbers", 1)[-1]
        before_stars = after.split("Lucky Stars", 1)[0]
        numbers = [int(x) for x in re.findall(r"\b\d{1,2}\b", before_stars)[:5]]
    if len(numbers) != 5:
        raise RuntimeError("Failed to extract 5 main numbers.")

    # 5) Lucky Stars: next two integers after "Lucky Stars"
    stars_label = section.find(string=re.compile(r"Lucky\s+Stars", re.I))
    if not stars_label:
        raise RuntimeError("Couldn't find 'Lucky Stars' label.")
    stars: List[int] = []
    for el in stars_label.find_all_next(True, limit=30):
        t = el.get_text(strip=True)
        if re.fullmatch(r"\d{1,2}", t):
            stars.append(int(t))
            if len(stars) == 2:
                break
        # stop at common breaks
        if isinstance(el.string, str) and re.search(r"View prize breakdown|\* \* \*", el.string):
            break
    if len(stars) != 2:
        # Fallback from text
        after = section_text.split("Lucky Stars", 1)[-1]
        stars = [int(x) for x in re.findall(r"\b\d{1,2}\b", after)[:2]]
    if len(stars) != 2:
        raise RuntimeError("Failed to extract 2 Lucky Stars.")

    return {
        "date": latest_date_iso,
        "numbers": numbers,
        "stars": stars,
        "jackpot_eur": jackpot_eur,
    }


def normalize_api_draw(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize fields from the v1/draws endpoint.
    Expected:
      - date: 'YYYY-MM-DD' (or ISO datetime); we coerce to YYYY-MM-DD when possible
      - numbers: list[int] (5)
      - stars: list[int] (2)
      - jackpot/prize in euros
    """
    # date
    date_val = (
        raw.get("date")
        or raw.get("draw_date")
        or raw.get("drawDate")
        or raw.get("draw_time")
    )
    if isinstance(date_val, str):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_val)
        if m:
            date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        else:
            try:
                date_iso = datetime.fromisoformat(date_val.replace("Z", "+00:00")).date().isoformat()
            except Exception:
                date_iso = date_val  # leave as-is
    else:
        date_iso = str(date_val) if date_val is not None else "unknown"

    # numbers
    numbers = raw.get("numbers") or raw.get("numbers_main") or raw.get("numbersList")
    if isinstance(numbers, str):
        numbers = [int(x) for x in re.findall(r"\d{1,2}", numbers)]
    numbers = list(numbers or [])

    # stars
    stars = raw.get("stars") or raw.get("lucky_stars") or raw.get("luckyStars")
    if isinstance(stars, str):
        stars = [int(x) for x in re.findall(r"\d{1,2}", stars)]
    stars = list(stars or [])

    # prize / jackpot
    prize = raw.get("prize") or raw.get("jackpot") or raw.get("jackpot_eur")
    if isinstance(prize, str):
        prize_eur = _parse_euro_to_int(prize)
    elif isinstance(prize, (int, float)) and not isinstance(prize, bool):
        prize_eur = int(prize)
    else:
        prize_eur = None

    return {
        "id": raw.get("id") or raw.get("draw_id") or raw.get("drawId"),
        "date": date_iso,
        "numbers": numbers,
        "stars": stars,
        "jackpot_eur": prize_eur,
        "raw": raw,
    }


def verify_latest(scraped: Dict[str, Any], api_latest: Dict[str, Any]) -> Dict[str, Any]:
    eq_date = (scraped["date"] == api_latest["date"])
    eq_numbers = (scraped["numbers"] == api_latest["numbers"])
    eq_stars = (scraped["stars"] == api_latest["stars"])
    # Jackpot may be missing on some API entries; only compare if present.
    eq_jackpot = (
        api_latest.get("jackpot_eur") is not None
        and scraped["jackpot_eur"] == api_latest.get("jackpot_eur")
    )
    return {
        "date_match": eq_date,
        "numbers_match": eq_numbers,
        "stars_match": eq_stars,
        "jackpot_match": eq_jackpot,
        "all_ok": (eq_date and eq_numbers and eq_stars),
    }


def render_html(out_path: str, context: Dict[str, Any]) -> None:
    latest = context["latest"]
    verdict = context["verification"]
    hist = context["history"]
    hist_count = len(hist)

    def balls_html(nums: List[int]) -> str:
        return "".join(f'<span class="ball">{n}</span>' for n in nums)

    def stars_html_fn(nums: List[int]) -> str:
        return "".join(f'<span class="star">{n}</span>' for n in nums)

    ok_text = "✅ Verified vs API" if verdict["all_ok"] else "⚠️ Mismatch with API"
    jacket = f"€{latest['jackpot_eur']:,}"

    # Build history table rows (avoid nested f-strings)
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

    numbers_html = balls_html(latest["numbers"])
    stars_html = stars_html_fn(latest["stars"])

    html_str = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EuroMillions Status</title>
<style>
  :root {{ --bg:#0b1220; --fg:#e8eefc; --muted:#9bb0d3; --card:#121a30; --ok:#2bc275; --warn:#f2c94c; }}
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
  .chip {{ padding: 4px 10px; border-radius: 999px; font-weight:600; }}
  .ok {{ background: rgba(43,194,117,.12); color: var(--ok); }}
  .warn {{ background: rgba(242,201,76,.12); color: var(--warn); }}
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
      <h1>EuroMillions — Latest</h1>
      <div class="muted">Last updated: {html.escape(context["timestamp"])} UTC</div>

      <div class="row" style="margin-top:16px;">
        <div class="stat">
          <div class="muted">Jackpot</div>
          <div class="jackpot">{jacket}</div>
        </div>
        <div class="chip {'ok' if verdict['all_ok'] else 'warn'}">{ok_text}</div>
      </div>

      <div class="row">
        <div>
          <div class="muted">Draw date</div>
          <div style="font-weight:700">{latest["date"]}</div>
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
        Sources: <a href="https://www.lottery.ie/results/euromillions/history">lottery.ie results » EuroMillions » history</a> ·
        <a href="https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc">euromillions.api.pedromealha.dev</a>
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
      <footer>Showing up to 200 latest draws from the API.</footer>
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
    ap.add_argument("--out-dir", default="site", help="Directory to write the static site into.")
    ap.add_argument("--api", default=EUROM_API_URL, help="Euromillions API endpoint.")
    args = ap.parse_args(argv)

    # 1) Scrape latest from lottery.ie (jackpot + last draw)
    page_html = fetch_html(LOTTERY_IE_HISTORY_URL)
    scraped_latest = scrape_latest_from_irish_history(page_html)

    # 2) Pull historical draws (desc)
    api_raw = fetch_json(args.api)
    if not isinstance(api_raw, list) or not api_raw:
        raise RuntimeError("Unexpected API response; expected non-empty list.")
    normalized_history = [normalize_api_draw(d) for d in api_raw]
    api_latest = normalized_history[0]

    # 3) Verify latest
    verification = verify_latest(scraped_latest, api_latest)

    now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    payload = {
        "timestamp": now_iso,
        "currentJackpotEUR": scraped_latest["jackpot_eur"],
        "lastDraw": scraped_latest,
        "verification": verification,
        "history": normalized_history,
        "sources": {
            "scrape": LOTTERY_IE_HISTORY_URL,
            "api": args.api,
        },
    }

    # 4) Write JSONs
    with open("euromillions.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": now_iso,
                "date": scraped_latest["date"],
                "jackpot_eur": scraped_latest["jackpot_eur"],
                "numbers": scraped_latest["numbers"],
                "stars": scraped_latest["stars"],
                "verified": verification["all_ok"],
            },
            f,
            indent=2,
        )

    # 5) Write static site
    out_html = os.path.join(args.out_dir, "index.html")
    render_html(out_html, {
        "timestamp": now_iso,
        "latest": scraped_latest,
        "verification": verification,
        "history": normalized_history,
    })

    print(f"✅ Wrote euromillions.json, latest.json and {out_html}")
    if not verification["all_ok"]:
        print("⚠️  Latest draw mismatch between scrape and API. See 'verification' fields.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
