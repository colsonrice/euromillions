#!/usr/bin/env python3
"""
update_euromillions.py  — API-only

- Fetches EuroMillions draws from Pedro Mealha's API (most recent first)
- Normalizes fields (date, numbers, stars, jackpot in EUR)
- Writes:
    * euromillions.json  (timestamp, currentJackpotEUR, lastDraw, history)
    * latest.json        (compact latest-only view)
    * site/index.html    (simple static status page)

Source API:
  https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc
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
from typing import Any, Dict, List, Optional

import requests

API_URL_DEFAULT = "https://euromillions.api.pedromealha.dev/v1/draws?limit=5000&sort=desc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; EuroMillionsFetcher/1.1; +github-actions)"
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


def _parse_euro_to_int(val: Any) -> Optional[int]:
    # Accept numeric or strings like "€26,800,624"
    if val is None:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val)
    if isinstance(val, str):
        digits = re.sub(r"[^\d]", "", val)
        return int(digits) if digits else None
    return None


def _as_numbers_list(v: Any) -> List[int]:
    if isinstance(v, list):
        out: List[int] = []
        for x in v:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    if isinstance(v, str):
        return [int(x) for x in re.findall(r"\d{1,2}", v)]
    return []


def normalize_draw(raw: Dict[str, Any]) -> Dict[str, Any]:
    # Date (prefer YYYY-MM-DD)
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

    jackpot_eur = _parse_euro_to_int(raw.get("jackpot") or raw.get("prize") or raw.get("jackpot_eur"))

    return {
        "id": raw.get("id") or raw.get("draw_id") or raw.get("drawId"),
        "date": date_iso or "unknown",
        "numbers": numbers[:5],  # ensure max 5
        "stars": stars[:2],      # ensure max 2
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


def render_html(out_path: str, context: Dict[str, Any]) -> None:
    latest = context["latest"]
    hist = context["history"]
    hist_count = len(hist)

    def balls_html(nums: List[int]) -> str:
        return "".join(f'<span class="ball">{n}</span>' for n in nums)

    def stars_html_fn(nums: List[int]) -> str:
        return "".join(f'<span class="star">{n}</span>' for n in nums)

    jackpot_fmt = f"€{latest['jackpot_eur']:,}" if isinstance(latest.get("jackpot_eur"), (int, float)) else "—"
    numbers_html = balls_html(latest.get("numbers", []))
    stars_html = stars_html_fn(latest.get("stars", []))

    # Build table rows
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
      <h1>EuroMillions — Latest (API)</h1>
      <div class="muted">Last updated: {html.escape(context["timestamp"])} UTC</div>

      <div class="row" style="margin-top:16px;">
        <div class="stat">
          <div class="muted">Jackpot</div>
          <div class="jackpot">{jackpot_fmt}</div>
        </div>
      </div>

      <div class="row">
        <div>
          <div class="muted">Draw date</div>
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
        Source: <a href="{html.escape(context['api'])}">euromillions.api.pedromealha.dev</a>
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
    ap.add_argument("--out-dir", default="site", help="Directory to write the static site into.")
    args = ap.parse_args(argv)

    # 1) Fetch & normalize from API only
    api_raw = fetch_json_with_retry(args.api, retries=3, backoff_sec=2.0)
    if not isinstance(api_raw, list) or not api_raw:
        raise RuntimeError("Unexpected API response; expected non-empty list.")

    normalized = [normalize_draw(d) for d in api_raw]
    # Ensure desc by date, just in case
    history = sort_desc_by_date(normalized)
    latest = history[0]

    current_jackpot = latest.get("jackpot_eur")
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    payload = {
        "timestamp": now_iso,
        "currentJackpotEUR": current_jackpot,
        "lastDraw": latest,
        "history": history,
        "sources": {
            "api": args.api,
        },
    }

    # 2) Write JSONs
    with open("euromillions.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": now_iso,
                "date": latest.get("date"),
                "jackpot_eur": current_jackpot,
                "numbers": latest.get("numbers", []),
                "stars": latest.get("stars", []),
                "verified": True,  # API is source of truth in this flow
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    # 3) Write static site
    out_html = os.path.join(args.out_dir, "index.html")
    render_html(out_html, {
        "timestamp": now_iso,
        "latest": latest,
        "history": history,
        "api": args.api,
    })

    print(f"✅ Wrote euromillions.json, latest.json and {out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
