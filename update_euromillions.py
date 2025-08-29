#!/usr/bin/env python3
"""
update_euromillions.py

Adds a scrape of the Irish National Lottery EuroMillions page to obtain the
CURRENT upcoming jackpot (e.g., "€40 Million Jackpot *"), while still using
Pedro Mealha's API for last draw + history.

Writes:
  * euromillions.json
      {
        "timestamp": "...Z",
        "currentJackpotEUR": <scraped euros>,
        "lastDraw": {... from API ...},
        "history": [... from API ...],
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
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

API_URL_DEFAULT = "https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc"
JACKPOT_URL_DEFAULT = "https://www.lottery.ie/draw-games/euromillions"

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


def _as_numbers_list(v: Any) -> List[int]:
    if isinstance(v, list):
        out: List[int] = []
        for x in v:
            try:
                out.append(int(x))
            except Exception:
                try:
                    out.append(int(str(x).strip()))
                except Exception:
                    pass
        return out
    if isinstance(v, str):
        return [int(x) for x in re.findall(r"\d{1,2}", v)]
    return []


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

    numbers = _as_numbers_list(raw.get("numbers") or raw.get("numbers_main"))
    stars = _as_numbers_list(raw.get("stars") or raw.get("lucky_stars"))
    jackpot_eur = extract_jackpot_eur(raw)

    return {
        "id": raw.get("id") or raw.get("draw_id") or raw.get("drawId"),
        "date": date_iso or "unknown",
        "numbers": numbers[:5],
        "stars": stars[:2],
        "jackpot_eur": jackpot_eur,
        "raw": raw,
    }


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
    ap.add_argument("--skip-scrape", action="store_true", help="Disable scraping and use API-only fallback for currentJackpotEUR.")
    ap.add_argument("--out-dir", default="site", help="Directory to write the static site into.")
    args = ap.parse_args(argv)

    # 1) Fetch & normalize from API
    api_raw = fetch_json_with_retry(args.api, retries=3, backoff_sec=2.0)
    if not isinstance(api_raw, list) or not api_raw:
        raise RuntimeError("Unexpected API response; expected non-empty list.")

    normalized = [normalize_draw(d) for d in api_raw]
    history = sort_desc_by_date(normalized)
    latest = history[0]
    latest_draw_jackpot = latest.get("jackpot_eur")

    # 2) Scrape current jackpot (next draw)
    scraped_eur: Optional[int] = None
    matched_text: Optional[str] = None
    if not args.skip_scrape:
        try:
            scraped_eur, matched_text = scrape_current_jackpot(args.jackpot_url)
        except Exception:
            # Silent fallback; we still produce output from API
            scraped_eur, matched_text = None, None

    # Choose current jackpot: prefer scraped; else fall back to last-draw jackpot
    current_jackpot = scraped_eur if scraped_eur is not None else latest_draw_jackpot
    current_src = "lottery.ie" if scraped_eur is not None else "api"

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
    if matched_text:
        payload["sources"]["currentJackpotText"] = matched_text

    # 3) Write JSONs
    with open("euromillions.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": now_iso,
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
        "timestamp": now_iso,
        "latest": latest,
        "history": history,
        "api": args.api,
        "currentJackpotEUR": current_jackpot,
        "currentJackpotSource": current_src,
        "jackpotPage": args.jackpot_url,
    })

    print("✅ Wrote euromillions.json, latest.json and", out_html)
    if scraped_eur is not None:
        print(f"ℹ️  Scraped jackpot: €{scraped_eur:,} from {args.jackpot_url} ({matched_text})")
    else:
        print("⚠️  Using API fallback for currentJackpotEUR (scrape unavailable).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
