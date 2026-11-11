#!/usr/bin/env python3
"""
score_grail.py — THE HOLY GRAIL 🏆

Points pointer: every runner in an 8+-runner race is scored across nine
criteria; the race sheet is shown highest-to-lowest and the top three are
the day's bets — each-way: half win, half place at 1/5 odds).

Scoring boxes (raw % goes in as points, missing data = empty box, never 0):

  Life%    horse lifetime wins ÷ runs, as %      (Sporting Life racecard)
  Trn%14   trainer 14-day win % (RaceMetrics in-form yards; empty otherwise)
  Jky%30   jockey 30-day win % (horseracing.net strike-rate widget)
  Fav      betting-forecast ladder, first five favs only:
           fav 33 · 2nd 17 · 3rd 11 · 4th 8 · 5th 6
  C+D      course & distance winner — 10 pts
  Dist     distance winner — 5 pts
  Crs      course winner — 3 pts
  TrnC%    trainer % at this course (HRN Top Course Trainers) — raw %
  JkyC%    jockey % at this course (HRN Top Course Jockeys) — raw %

C+D tiers first (Sporting Life itself never reports C+D together with the
other two); a non-C+D horse can stack Dist (5) and Crs (3), max 8 — the
tier ladder the 10-5-3 implies.

Same racecard payload that theorists use in the evening carries
finish_position + placed-ride count, so ONE script scores the morning,
and the evening rerun fills every runner's Pos + WIN/PLACE/LOSE. The
top-3 targets are marked in gold.

Usage
-----
    python3 score_grail.py                       # today (Europe/London)
    python3 score_grail.py --date 2026-11-06     # rerun / settle a day
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
DRAFT_DIR = BASE_DIR / "drafts"
DESKTOP_DIR = Path.home() / "Desktop" / "Holy Grail"
GRAIL_APP_DIR = os.environ.get("GRAIL_APP_DIR")   # Actions/CI override
APP_DIR = Path(GRAIL_APP_DIR or Path.home() / "projects" / "holy-grail")
COMMIT_NAME = "jobsaboba"
COMMIT_EMAIL = "160133851+jobsaboba@users.noreply.github.com"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
HRN_UA = ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) "
          "Gecko/20100101 Firefox/133.0")   # horseracing.net needs a browser UA

SL = "https://www.sportinglife.com"
RM = "https://www.racemetrics.co.uk"
HRN = "https://www.horseracing.net"

MIN_RUNNERS = 8
COURSE_COUNTRIES = {"ENG", "IRE", "EIRE", "WAL", "WLS", "SCO", "SCT"}
FAV_POINTS = {1: 33, 2: 17, 3: 11, 4: 8, 5: 6}   # first five favs only
CD_PTS, DIST_PTS, CRS_PTS = 10, 5, 3
TOP3 = 3

_LAST_FETCH = [0.0]


# --------------------------------------------------------------------------
# http + cache
# --------------------------------------------------------------------------
def fetch(url: str, cache_name: str, refresh: bool = False,
          delay: float = 0.35, ua: str | None = None) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / cache_name
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8", errors="ignore")

    headers = {"User-Agent": ua or UA, "Accept-Language": "en-GB,en"}
    wait = delay - (time.time() - _LAST_FETCH[0])
    if wait > 0:
        time.sleep(wait)

    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=40)
            _LAST_FETCH[0] = time.time()
            if r.status_code == 200 and len(r.text) > 20_000:
                path.write_text(r.text, encoding="utf-8")
                return r.text
            last_err = f"HTTP {r.status_code} ({len(r.text)} bytes)"
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)
        time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def london_today() -> str:
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def strip_accents(s: str) -> str:
    return (unicodedata.normalize("NFKD", s).encode("ascii", "ignore")
            .decode("ascii"))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents((s or "").lower())).strip()


def frac_to_dec(odds: str | None) -> float | None:
    if not odds:
        return None
    o = odds.strip().lower().replace(" ", "")
    if o in {"sp", "-", "", "n/a"}:
        return None
    if o.startswith("evs"):
        return 2.0
    m = re.fullmatch(r"(\d+)/(\d+)", o)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return 1 + a / b if b else None
    try:
        return float(o)
    except ValueError:
        return None


def next_data(html: str) -> dict:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S,
    )
    if not m:
        raise ValueError("no __NEXT_DATA__ payload")
    return json.loads(m.group(1))


# --------------------------------------------------------------------------
# Sporting Life — the whole day's cards
# --------------------------------------------------------------------------
def parse_forecast_order(text: str | None) -> list[str]:
    """'A (4/1), B (9/2)' -> ['a', 'b'] — the bookmaker's favourite order."""
    if not text:
        return []
    out = []
    for chunk in text.split(","):
        m = re.match(r"\s*(.+?)\s*\(([^)]+)\)\s*$", chunk)
        if m:
            out.append(norm(m.group(1)))
    return out


def scrape_races_index(dstr: str, refresh: bool) -> list[dict]:
    """Race list for the exact date. 'today' also works via /racecards, but
    SL ignores the date param there and serves today anyway — so verify the
    meeting dates, and fall back to /results/<iso-date> for replay/settled
    days (same payload shape)."""
    for base in ("racecards", "results"):
        try:
            html = fetch(f"{SL}/racing/{base}/{dstr}",
                         f"gr-index-{base}-{dstr}.html", refresh)
            meetings = next_data(html)["props"]["pageProps"].get("meetings") or []
        except Exception:
            continue
        out = []
        for mm in meetings:
            ms = mm.get("meeting_summary") or {}
            # race items on /racecards omit country_short_name — the meeting
            # summary always carries it, so use that as the fallback
            mco = (((ms.get("course") or {}).get("country") or {})
                   .get("short_name"))
            for rc in mm.get("races") or []:
                if rc.get("date") != dstr:
                    continue
                if rc.get("ride_count") and int(rc["ride_count"]) < MIN_RUNNERS:
                    continue   # cheap pre-filter; the card itself is the authority
                if (((rc.get("country_short_name") or mco) or "").upper()
                        not in COURSE_COUNTRIES):
                    continue   # strict: UK/IRE only, no default fallback
                ref = rc.get("race_summary_reference") or {}
                out.append({
                    "race_id": ref.get("id"),
                    "meeting": rc.get("course_name"),
                    "time": rc.get("time"),
                    "race_name": rc.get("name"),
                    "class": rc.get("race_class"),
                    "distance": rc.get("distance"),
                    "going": rc.get("going") or ms.get("going"),
                })
        if out:
            if base == "results":
                print(f"  (index: served from the results endpoint — "
                      f"{len(out)} races for {dstr})")
            return out
    raise RuntimeError(f"No race index available for {dstr}")


def fetch_racecard(dstr: str, race: dict, refresh: bool) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", (race["meeting"] or "").lower()).strip("-")
    url = f"{SL}/racing/racecards/{dstr}/{slug}/racecard/{race['race_id']}/x"
    html = fetch(url, f"gr-rc-{race['race_id']}.html", refresh)
    card = next_data(html)["props"]["pageProps"].get("race")
    if not card:
        raise RuntimeError(f"no race payload for {race['race_id']}")

    fav_order = parse_forecast_order(card.get("betting_forecast"))
    rides = []
    for rd in card.get("rides", []):
        name = (rd.get("horse") or {}).get("name", "")
        life = next(iter(rd.get("horse_lifetime_stats") or []), {})
        hist = {h.get("type"): int(h.get("value") or 0)
                for h in (rd.get("race_history_stats") or [])}
        rides.append({
            "horse": name,
            "trainer": (rd.get("trainer") or {}).get("name", ""),
            "jockey": (rd.get("jockey") or {}).get("name", "") or "",
            "odds": (rd.get("betting") or {}).get("current_odds"),
            "stars": rd.get("timeform_stars"),
            "rating123": rd.get("rating123"),
            "cloth": rd.get("cloth_number"),
            "status": (rd.get("ride_status") or "RUNNER").upper(),
            "comment": (rd.get("commentary") or "").strip(),
            "runs": int(life.get("run_count") or 0) or None,
            "wins": int(life.get("win_count") or 0) or None,
            "fav_rank": (fav_order.index(norm(name)) + 1
                         if norm(name) in fav_order else None),
            "cd": hist.get("CourseDistance"),
            "dist": hist.get("Distance"),
            "crs": hist.get("Course"),
            "pos": rd.get("finish_position"),
        })
    return {
        **race,
        "placed": card.get("number_of_placed_rides"),
        "runs": rides,
    }


# --------------------------------------------------------------------------
# RaceMetrics — trainer 14-day win %
# --------------------------------------------------------------------------
def scrape_rm_trainers(refresh: bool, dstr: str) -> dict[str, float]:
    cache = CACHE_DIR / "gr-rm-trainers.html"
    try:
        fresh = (datetime.fromtimestamp(
            cache.stat().st_mtime, tz=ZoneInfo("Europe/London")
        ).date().isoformat() == dstr)
    except OSError:
        fresh = False
    html = fetch(f"{RM}/trainers-in-form", "gr-rm-trainers.html",
                 refresh or not fresh)
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, float] = {}
    for table in soup.find_all("table"):
        header = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if "today" not in header or "trainer" not in header:
            continue
        rows = table.find_all("tr")[1:]
        for tr in rows:
            c = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(c) < 9:
                continue
            try:
                out[norm(c[1])] = float(c[4].replace("%", "") or 0)
            except ValueError:
                pass
    return out


# --------------------------------------------------------------------------
# horseracing.net — jockey 30-day %, top course jockeys/trainers
# --------------------------------------------------------------------------
TITLE_WORDS = {"mr", "miss", "mrs", "ms", "dr", "sir"}


def initials_match(np: list[str], sp: list[str]) -> bool:
    """'S Bowen' vs 'sean-bowen': full-name tail + leading-initial front."""
    tail = 0
    while tail < len(np) and tail < len(sp) and np[-1 - tail] == sp[-1 - tail]:
        tail += 1
    rest = np[: len(np) - tail]
    if not rest:
        return True
    return (all(len(p) == 1 for p in rest) and
            len(rest) >= len(sp) - tail and
            all(rest[i][0] == sp[i][0] for i in range(len(sp) - tail)))


def hrn_by_course(course_tbl: dict[str, dict], course: str, name: str
                  ) -> float | None:
    """HRN name → SL name, exact first then initials-tolerant."""
    if not course or not name:
        return None
    tbl = course_tbl.get(course)
    if not tbl:
        return None
    if name in tbl:
        return tbl[name][2]
    np = [p for p in re.findall(r"[a-z0-9]+", name) if p not in TITLE_WORDS]
    hit = None
    for hname, rec in tbl.items():
        sp = re.findall(r"[a-z0-9]+", hname)
        if np and sp and initials_match(np, sp):
            hit = rec
            break
    return hit[2] if hit else None


def fetch_hrn_jockeys(names: list[str], dstr: str, refresh: bool) -> dict[str, float]:
    """HRN per-jockey Strike Rate (Last 30 Days) widget, keyed by SL name."""
    if not names:
        return {}
    try:
        index = fetch(f"{HRN}/jockeys", f"gr-hrn-index-{dstr}.html", refresh,
                      ua=HRN_UA)
    except Exception as exc:  # noqa: BLE001
        # HRN blocks datacenter IPs (GitHub Actions runners) — fall back to
        # the resolved 30-day seed the desktop publishes into the repo
        seed = _seed_path(f"hrn-jky30-{dstr}.json")
        if seed.exists():
            data = json.loads(seed.read_text(encoding="utf-8"))
            print(f"  HRN jockeys blocked ({exc}) — using repo snapshot "
                  f"({len(data)} jockeys)", file=sys.stderr)
            return {k: float(v) for k, v in data.items()}
        print(f"  HRN jockey index unavailable ({exc}) — "
              f"Jky%30 boxes empty today", file=sys.stderr)
        return {}
    slugs = sorted(set(re.findall(r'href="/jockeys/([a-z0-9-]+)"', index)))

    def slug_guess(name: str) -> str:
        s = strip_accents(name).lower().replace("'", "").replace(".", "")
        return re.sub(r"[^a-z0-9]+", "-", s).strip("-")

    sr: dict[str, float] = {}
    for name in sorted({n for n in names if n}):
        direct = slug_guess(name)
        np = [p for p in re.findall(r"[a-z0-9]+", name.lower())
              if p not in TITLE_WORDS]
        cand = [direct] if direct in slugs else [
            s for s in slugs
            if re.findall(r"[a-z0-9]+", s) and np and initials_match(np, re.findall(r"[a-z0-9]+", s))
        ]
        if not cand:
            continue   # jockey not on HRN today — box stays empty
        try:
            page = fetch(f"{HRN}/jockeys/{cand[0]}",
                         f"gr-hrn-jky-{dstr}-{cand[0]}.html", refresh, ua=HRN_UA)
        except Exception:
            continue
        i = page.find("Strike Rate (Last 30 Days)")
        if i < 0:
            continue
        vals = re.findall(r'row-title-text">\s*([\d.]+)', page[i:i + 4000])
        if len(vals) >= 6:
            try:
                sr[name] = float(vals[5])   # win %
            except ValueError:
                pass
    if sr:   # keep the day's resolved seed topped up (merge: never shrinks
        # when one desktop run's transient fetch fails mid-list)
        seed = _seed_path(f"hrn-jky30-{dstr}.json")
        merged: dict = {}
        if seed.exists():
            try:
                merged = json.loads(seed.read_text(encoding="utf-8"))
            except ValueError:
                merged = {}
        merged.update(sr)
        _write_seed(f"hrn-jky30-{dstr}.json", merged)
    return sr


def _sr(rec) -> float | None:
    """A hot-table record (wins, runs, sr) → just the strike % as points."""
    if isinstance(rec, tuple):
        return rec[2]
    return rec if isinstance(rec, (int, float)) else None


def hrn_name_match(tbl: dict, name: str):
    """HRN name → record: exact key first, then initial-tolerant."""
    if not name or not tbl:
        return None
    if name in tbl:
        return tbl[name]
    np = [p for p in re.findall(r"[a-z0-9]+", name) if p not in TITLE_WORDS]
    if not np:
        return None
    for hname, rec in tbl.items():
        sp = re.findall(r"[a-z0-9]+", hname)
        if sp and initials_match(np, sp):
            return rec
    return None


def _combo_sr(combos: dict, course: str, trainer: str, jockey: str
              ) -> float | None:
    """Trainer/Jockey combo strike at today's course."""
    tbl = combos.get(course)
    if not tbl or not trainer or not jockey:
        return None
    pair = (trainer, jockey)
    if pair in tbl:
        return tbl[pair]
    tnp = [p for p in re.findall(r"[a-z0-9]+", trainer) if p not in TITLE_WORDS]
    jnp = [p for p in re.findall(r"[a-z0-9]+", jockey) if p not in TITLE_WORDS]
    if not tnp or not jnp:
        return None
    for (tr, jk), sr in tbl.items():
        trp = re.findall(r"[a-z0-9]+", tr)
        jkp = re.findall(r"[a-z0-9]+", jk)
        if trp and jkp and initials_match(tnp, trp) and initials_match(jnp, jkp):
            return sr
    return None


def fetch_hrn_stats(dstr: str, refresh: bool) -> dict:
    """HRN /stats tables — Hot Trainers, Hot Jockeys, Top Course
    Jockeys/Trainers, Top Trainer/Jockey Combos. Returns:
      ht      : name → (wins, runs, sr)          hot trainers
      hj      : name → (wins, runs, sr)          hot jockeys
      cj / ct : {course: {name: (wins, runs, sr)}}
      combos  : {course: {(trainer, jockey): sr}}
    Only names with 3+ wins are listed by HRN — a miss = empty box.
    Datacenter IPs (GitHub Actions) get 403 from HRN's CDN — those runs
    fall back to the snapshot your desktop publishes into the repo."""
    try:
        page = fetch(f"{HRN}/stats", f"gr-hrn-stats-{dstr}.html", refresh,
                     ua=HRN_UA)
        out = _parse_hrn_stats_tables(page)
        if not out["cj"] and not out["ct"] and not refresh:
            # HRN builds these tables on race-day mornings — an empty
            # cached copy from an earlier run gets one uncached retry
            page = fetch(f"{HRN}/stats", f"gr-hrn-stats-{dstr}.html", True,
                         ua=HRN_UA)
            out = _parse_hrn_stats_tables(page)
        return out
    except Exception as exc:  # noqa: BLE001
        seed = APP_DIR / "data" / f"hrn-stats-{dstr}.html"
        if seed.exists():
            print(f"  HRN blocked ({exc}) — using repo snapshot {seed.name}")
            return _parse_hrn_stats_tables(read_text(seed))
        print(f"  HRN unavailable ({exc}) — HRN boxes empty today",
              file=sys.stderr)
        return {"ht": {}, "hj": {},
                "cj": {}, "ct": {}, "combos": {}}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _hrn_section(page: str, heading: str) -> str:
    i = page.find(heading)
    if i < 0:
        return ""
    frag = page[i: i + 120_000]
    stop = frag.find("<h2", 10)
    return frag[:stop] if stop > 0 else frag


def _hot_rows(frag: str) -> dict:
    """name → (wins, runs, sr) for a single-name-per-row HRN table."""
    tbl = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", frag, re.S):
        cells = [norm(re.sub(r"<[^>]+>", " ", c))
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 4 or not cells[0]:
            continue
        name = re.split(r"\s+wins:", cells[0], flags=re.I |
                        re.A)[0].strip()              # drop the mobile echo
        if not name or not re.fullmatch(r"\d+", cells[1] or ""):
            continue
        sr = float(cells[3].replace("%", "")) if cells[3] else None
        tbl[name] = (int(cells[1]), int(cells[2] or 0), sr)
    return tbl


def _combo_rows(frag: str) -> dict:
    """{course: {(trainer, jockey): sr}} from the combo table — cells are
    [course echo, trainer, trn %, jockey, jky %, TEAM strike]."""
    out = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", frag, re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 6 or not cells[1] or not cells[3]:
            continue
        course = re.split(r"trainer\s*:", cells[0], flags=re.I | re.A)[0]
        try:
            sr = float(cells[5].replace("%", ""))
        except ValueError:
            continue
        out.setdefault(norm(course), {})[(norm(cells[1]), norm(cells[3]))] = sr
    return out


def _parse_hrn_stats_tables(page: str) -> dict:
    cj, ct = _parse_hrn_course_tables(page)   # course tables: 5-cell shape
    return {
        "ht": _hot_rows(_hrn_section(page, "Hot Trainers")),
        "hj": _hot_rows(_hrn_section(page, "Hot Jockeys")),
        "cj": cj, "ct": ct,
        "combos": _combo_rows(_hrn_section(page, "Top Trainer/Jockey Combo")),
    }


def _parse_hrn_course_tables(page: str) -> tuple[dict, dict]:
    out = {}
    for heading, key in (("Top Course Jockeys", "cj"), ("Top Course Trainers", "ct")):
        out[key] = {}
        i = page.find(heading)
        if i < 0:
            continue
        frag = page[i: i + 120_000]
        stop = frag.find("<h2", 10)
        if stop > 0:
            frag = frag[:stop]
        tbl: dict[str, dict] = out[key]
        # one <tr> per entry: cell 0 = name (+ a responsive mobile echo of
        # 'Course: X Wins: N Runs: M'), cells 1..4 = course, wins, runs, sr%
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", frag, re.S):
            cells = [norm(re.sub(r"<[^>]+>", " ", c))
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) < 5 or not cells[0]:
                continue
            name = norm(cells[0].split("course:")[0])   # drop the mobile echo
            course, wins, runs = cells[1], cells[2], cells[3]
            if not name or not course or not re.fullmatch(r"\d+", wins or ""):
                continue
            sr = float(cells[4].replace("%", "")) if cells[4] else None
            tbl.setdefault(course, {})[name] = (int(wins), int(runs or 0), sr)
    return out["cj"], out["ct"]


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
def score_race(race: dict, trn14: dict[str, float], jky30: dict[str, float],
               course_jk: dict, course_tr: dict, hot_tr: dict | None = None,
               hot_jk: dict | None = None, combos: dict | None = None) -> list[dict]:
    runners = [r for r in race["runs"] if r["status"] == "RUNNER"]
    meeting = norm(race["meeting"])
    hot_tr = hot_tr or {}
    hot_jk = hot_jk or {}
    combos = combos or {}
    sc = []
    for r in runners:
        live = (r["wins"] is not None and r["runs"] and r["runs"] > 0)
        life = (round(100 * r["wins"] / r["runs"]) if live else None)
        t14 = trn14.get(norm(r["trainer"]))
        j30 = jky30.get(r["jockey"])
        # HRN /stats hot lists — raw strike-rate % becomes points
        ht = _sr(hrn_name_match(hot_tr, norm(r["trainer"])))
        hj = _sr(hrn_name_match(hot_jk, norm(r["jockey"])))
        tjc = _combo_sr(combos, meeting, norm(r["trainer"]), norm(r["jockey"]))

        fav = None
        if r["fav_rank"]:
            fav = FAV_POINTS.get(r["fav_rank"])

        cd = emb = crs = None
        if r["cd"]:                       # C+D winner: Sporting Life reports
            cd = CD_PTS                   # CD alone — tier stops here
        else:
            if r["dist"]:
                emb = DIST_PTS
            if r["crs"]:
                crs = CRS_PTS

        trc = hrn_by_course(course_tr, meeting, norm(r["trainer"]))
        cjk = hrn_by_course(course_jk, meeting, norm(r["jockey"]))

        total = sum(p for p in (life, t14, j30, ht, hj, tjc, fav, cd, emb,
                                crs, trc, cjk)
                    if p is not None)
        # settled?
        pos = r["pos"]
        placed = race["placed"] if race["placed"] else 3
        if r["status"] != "RUNNER":
            res = "NR"
        elif pos in (None, 0):
            res = None                    # not off yet
        elif int(pos) == 1:
            res = "WIN"
        elif int(pos) <= int(placed):
            res = "PLACE"
        else:
            res = "LOSE"

        sc.append({
            **r,
            "life_pts": life, "trn_pts": t14, "jky_pts": j30,
            "ht_pts": ht, "hj_pts": hj, "tjc_pts": tjc,
            "fav_pts": fav,
            "cd_pts": cd, "dist_pts": emb, "crs_pts": crs, "trnc_pts": trc,
            "cjkc_pts": cjk, "total": total, "res": res, "pos": pos,
        })
    sc.sort(key=lambda r: (
        -r["total"],
        r["fav_rank"] or 99,
        -(r["life_pts"] or 0),
        frac_to_dec(r["odds"]) or 999,
        r["cloth"] or 99,
    ))
    return sc


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------
BOXES = [("life_pts", "Life%"), ("trn_pts", "Trn%14"), ("jky_pts", "Jky%30"),
         ("ht_pts", "HT%"), ("hj_pts", "HJ%"), ("tjc_pts", "T·J%"),
         ("fav_pts", "Fav"), ("cd_pts", "C+D"), ("dist_pts", "Dist"),
         ("crs_pts", "Crse"), ("trnc_pts", "TrnC%"), ("cjkc_pts", "JkyC%")]
BOX_COLS = ["Win", "Place", "Loss", "Return", "Bank"]

# ----------------------------- bank: £10 start, 1% on each of the top three
GRAIL_START_BANK = 10.00
GRAIL_STAKE_PCT = 0.01
GRAIL_EW_PLACE_FRACTION = 0.2     # place part pays ⅕ the odds (365 each-way)
_BANK: dict[str, tuple] = {}        # key day|time|meeting|horse → (stake,profit,bank_after)
_BANK_DAYS: dict[str, tuple] = {}   # day → (bank_in, bank_out)


def bet_return(odds_txt: str | None, res: str | None, stake: float,
               pos: int | None = None) -> float | None:
    """Profit on the TOTAL stake `stake` for the Grail's each-way top-three
    bets: 1% split ½ win + ½ place, the place half paying ⅕ the odds on
    the race's own place count (extra-place races pay deeper for free).
    WIN = winning half at the full price + placing half at ⅕; PLACE = the
    placing half pays at ⅕ (winning half lost); LOSE = the lot. Unsettled
    → None (the bank doesn't move until results are in)."""
    if not res or res == "NR":
        return None
    dec = frac_to_dec(odds_txt) if odds_txt else None
    if res == "LOSE" or dec is None:     # settled with no price → the lot
        return round(-stake, 2)
    win_half = place_half = stake / 2
    win_profit = win_half * (dec - 1)
    place_profit = place_half * (dec - 1) * GRAIL_EW_PLACE_FRACTION
    if res == "WIN":
        return round(win_profit + place_profit, 2)
    if res == "PLACE":
        return round(-win_half + place_profit, 2)
    return None


def _bank_walk(j: dict, bank: float) -> float:
    """Walk one day payload's settled races in time order, staking 1% of
    the race-start bank on each of the top three (each-way at 1/5 odds).
    Stakes don't exist until the race is off and settled."""
    day = j["date"]
    st = _BANK_DAYS.setdefault(day, [bank, bank])
    st[0] = bank
    for rc in sorted(j.get("races") or [], key=lambda c: c["time"]):
        cands = rc.get("runners") or rc.get("scored") or []
        rows = [r for i, r in enumerate(cands)
                if (r.get("rank") or i + 1) <= TOP3][:TOP3]
        if len(rows) < 3:
            continue                             # short/payload-day — no bets
        if any(r.get("res") not in ("WIN", "PLACE", "LOSE", "NR") for r in rows):
            continue                             # a bet still pending — no money
        betrows = [r for r in rows if r.get("res") != "NR"]
        if not betrows:
            continue                             # the whole trio is NR — no bets
        stake = round(bank * GRAIL_STAKE_PCT, 2)
        stake = max(stake, 0.01)           # Ladbrokes takes 1p stakes, so the
        bets, net = [], 0.0                # 1% rule survives small banks
        for r in betrows:                  # NR picks are void — stake returned
            prof = bet_return(r.get("odds"), r.get("res"), stake, r.get("pos"))
            if prof is None:               # settled with no price → the lot
                prof = round(-stake, 2)
            bets.append((r, prof))
            net = round(net + prof, 2)
        bank = round(bank + net, 2)        # bank moves once per race, so all
        for r, prof in bets:               # three rows show the race-END bank
            _BANK[f"{day}|{rc['time']}|{rc['meeting']}|{r['horse']}"] = (
                stake, round(prof, 2), bank)
        st[1] = bank
    return bank


def grail_bank_replay(data_dir: Path, current_day: str,
                      cards: list[dict]) -> float:
    """Rebuild the whole £10-start ledger in strict day order across every
    stored day file. The day being published walks its FRESH cards (its
    stored file is stale mid-publish); every other day — INCLUDING days
    after it, so a backdated republish still chains the full history —
    walks its stored payload. Returns the end bank."""
    _BANK.clear()
    _BANK_DAYS.clear()
    bank = GRAIL_START_BANK
    days = sorted(set(p.stem for p in data_dir.glob("????-??-??.json"))
                  | {current_day})
    for d in days:
        if d == current_day:
            j = {"date": current_day, "races": cards}
        else:
            try:
                j = json.loads((data_dir / f"{d}.json").read_text(
                    encoding="utf-8"))
            except Exception:
                continue
        bank = _bank_walk(j, bank)
    return bank


def bank_key(day: str, time: str, meeting: str, horse: str) -> str:
    return f"{day}|{time}|{meeting}|{horse}"


def box(v):
    return "" if v is None else (str(int(v)) if float(v).is_integer()
                                 else f"{v:.1f}")


def write_html(dstr: str, racedays: list[dict]) -> Path:
    races = racedays
    cards = []
    for rc in races:
        rows = ""
        for rank, r in enumerate(rc["scored"], start=1):
            tops = rank <= TOP3
            boxes = "".join(
                f'<td class="b">{box(r[k]) if r.get(k) is not None else ""}</td>'
                for k, _ in BOXES
            )
            res = ""
            if rc.get("settled") or (r.get("res") not in (None,)):
                mark = {None: "—", "WIN": "Wins ✅", "PLACE": "Place 🟢",
                        "LOSE": "Lose ❌", "NR": "NR"}.get(r["res"], "—")
                res = (f'<td class="pos">pos {r["pos"]}</td>'
                       f'<td class="res r{r["res"]}">{mark}</td>')
            else:
                res = '<td class="pos">—</td><td class="res">—</td>'
            money = ""
            if tops and r.get("stake") is not None:
                prof = r.get("bet_profit")
                sign = "+" if prof >= 0 else ""
                money = (f'<td class="stake">£{r["stake"]:.2f}</td>'
                         f'<td class="res p{1 if prof >= 0 else 0}">'
                         f'{sign}£{prof:.2f}</td>'
                         f'<td class="stake">£{r["bank_after"]:.2f}</td>')
            else:
                money = '<td class="stake"></td><td></td><td></td>'
            rows += (
                f'<tr class="{"top3" if tops else ""}{" nr" if r["res"]=="NR" else ""}">'
                f'<td class="rk">{rank}</td>'
                f'<td class="hr"><b>{r["horse"]}</b> <span class="stars">'
                f'{"★" * (r["stars"] or 0)}</span></td>'
                f'<td class="tj">{r["jockey"] or ""}</td>'
                f'<td class="tj">{r["trainer"]}</td>'
                f'<td class="odds">{r["odds"] or ""}</td>'
                f'{boxes}{res}{money}'
                f'<td class="tot">{box(r["total"])}</td></tr>'
            )
        cards.append(
            f'<div class="race"><div class="rh"><span>{rc["time"]} '
            f'{rc["meeting"]}</span><span class="meta">{rc["race_name"]} · '
            f'Class {rc["class"]} · {rc["distance"]} · {rc["going"] or ""}'
            f' · {len(rc["scored"])} runners</span></div>'
            f'<table><thead><tr><th>#</th><th>Horse</th><th>Jockey</th>'
            f'<th>Trainer</th><th>Odds</th>'
            + "".join(f"<th>{lbl}</th>" for _, lbl in BOXES)
            + "<th>Pos</th><th>Res</th><th>Stk</th><th>£</th><th>Bank</th><th>Σ</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>The Holy Grail</title><style>"
        "body{background:#0b1220;color:#e7eef9;font-family:system-ui,sans-serif;"
        "margin:0 auto;max-width:1100px;padding:18px}"
        "h1{margin:0 0 4px;font-size:1.6rem}.sub{color:#8fa3bf;font-size:.85rem;margin-bottom:16px}"
        ".race{background:#131c2e;border:1px solid #22304a;border-radius:16px;"
        "padding:14px;margin-bottom:14px;overflow-x:auto}"
        ".rh{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px;"
        "font-weight:800}.rh span:first-child{color:#38bdf8}.meta{color:#8fa3bf;"
        "font-weight:600;font-size:.72rem;text-align:right}"
        "table{width:100%;border-collapse:collapse;font-size:.8rem}"
        "th{color:#8fa3bf;text-align:left;font-size:.68rem;letter-spacing:.5px;"
        "border-bottom:1px solid #22304a;padding:4px 6px;white-space:nowrap}"
        "td{padding:4px 6px;border-bottom:1px solid #1b2540;white-space:nowrap}"
        "td.hr b{font-size:.92rem}.stars{color:#fbbf24;letter-spacing:1px}"
        "td.tj{color:#8fa3bf;font-size:.72rem}td.odds{font-weight:700;color:#38bdf8}"
        "td.rk{font-weight:900;color:#8fa3bf}td.b{text-align:center;width:44px;"
        "background:#0f172a}td.tot{font-weight:900;color:#34d399;font-size:.95rem}"
        "tr.top3 td{background:rgba(251,191,36,.07)}tr.top3 td.rk{color:#fbbf24}"
        "td.pos,td.res{color:#8fa3bf}td.res.rWIN{color:#34d399;font-weight:700}"
        "td.res.rPLACE{color:#86efac}td.res.rLOSE{color:#ef4444}"
        "td.stake{color:#fbbf24;font-variant-numeric:tabular-nums;text-align:right}"
        "td.res.p1{color:#34d399!important}td.res.p0{color:#ef4444!important}"
        "tr.nr{opacity:.45}.foot{color:#8fa3bf;font-size:.7rem;margin-top:18px;line-height:1.6}"
        "</style></head><body>"
        f"<h1>🏆 The Holy Grail</h1>"
        f"<div class='sub'>{dstr} · 8+ runner races only · sorted by score · "
        f"top {TOP3} = the bets (each-way: ½ win · ½ place at 1/5 odds)</div>"
        + "".join(cards))
    bs, be = _BANK_DAYS.get(dstr, (GRAIL_START_BANK, GRAIL_START_BANK))
    if be != GRAIL_START_BANK:
        html += (f"<div class='foot'><b style='color:#fbbf24'>💰 BANK "
                 f"£{bs:.2f} → £{be:.2f}</b> — £10 start · 1% of the bank "
                 "on each of the top three per race · each-way 1/5 odds · "
                 "races still to run stake nothing.</div>")
    html += ("<div class='foot'>Points go in raw: any % = that many points. "
        "Empty box = no data (never counted as 0).<br>"
        "Fav ladder: 1st 33 · 2nd 17 · 3rd 11 · 4th 8 · 5th 6 (from the "
        "Sporting Life betting forecast). C+D win 10 · distance win 5 · "
        "course win 3 (tiered).<br>"
        "Trn%14 = RaceMetrics yards in form (14d win%); Jky%30 + course "
        "stats = horseracing.net. Trainer/jockey course % only listed when "
        "HRN shows 3+ course wins.</div>"
        "</body></html>"
    )
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    out = DESKTOP_DIR / f"The Holy Grail {dstr}.html"
    out.write_text(html, encoding="utf-8")
    return out


def write_xlsx(dstr: str, racedays: list[dict]) -> list[Path]:
    sys.path.insert(0, str(BASE_DIR))
    from xlsx_lite import write_xlsx

    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    header = (["Date", "Meeting", "Time", "Runners", "Top3", "Horse", "Stars",
               "Odds", "Jockey", "Trainer"]
              + [lbl for _, lbl in BOXES] + ["Total"]
              + ["Pos", "Result", "Stake £", "Return £", "Bank £", "Comment"])
    rows = []
    for rc in racedays:
        for rank, r in enumerate(rc["scored"], start=1):
            if rank <= TOP3 and r.get("stake") is not None:
                prof = r.get("bet_profit")
                sign = "+" if prof >= 0 else ""
                money = [f"{r['stake']:.2f}", f"{sign}{prof:.2f}",
                         f"{r['bank_after']:.2f}"]
            else:
                money = ["", "", ""]
            rows.append(
                [rc["date"], rc["meeting"], rc["time"], len(rc["scored"]),
                 "Y" if rank <= TOP3 else "", r["horse"], r["stars"],
                 r["odds"], r["jockey"], r["trainer"]]
                + [box(r[k]) for k, _ in BOXES]
                + [box(r["total"]), r["pos"] if r["pos"] else "",
                   r["res"] or ""]
                + money
                + [r["comment"][:120]]
            )
    day = DESKTOP_DIR / f"The Holy Grail {dstr}.xlsx"
    write_xlsx(day, header, rows,
               widths=[11, 12, 7, 9, 6, 22, 6, 7, 16, 20, 8, 8, 8, 6, 6, 6, 6, 8, 8, 8, 8, 8, 8, 6, 8, 60, 8, 9, 9],
               sheet="scorecards")

    rolling_store = CACHE_DIR / "grail_all.csv"
    import csv
    all_rows: dict[tuple, list] = {}
    if rolling_store.exists():
        with open(rolling_store, newline="", encoding="utf-8") as fh:
            for rec in csv.reader(fh):
                if rec and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec[0] or ""):
                    all_rows[(rec[0], rec[2], rec[5])] = (rec
                                                          + [""] * (29 - len(rec)))[:29]
    for r in rows:
        all_rows[(r[0], r[2], r[5])] = [str(c) for c in r]
    allfile = DESKTOP_DIR / "The Holy Grail — all days.xlsx"
    merged = [all_rows[k] for k in sorted(all_rows)]
    # ── money columns stamped straight off the payload bank walk — the
    # same £10 chain the app + day files use — so the book can never
    # disagree with them. (Recomputing money over merged rows failed: the
    # archive carries old-schema rows with picks from previous scoring
    # eras. The walk's own ledger is the single source of truth.)
    gd_bank = grail_bank_replay(APP_DIR / "data", dstr, racedays)
    covered = set(_BANK_DAYS) | {dstr}
    for r in merged:
        if r[0] in covered:
            r[25] = r[26] = r[27] = ""
            hit = _BANK.get(bank_key(r[0], r[2], r[1], r[5]))
            if hit:
                prof = hit[1]
                r[25] = f"{hit[0]:.2f}"
                r[26] = f"+{prof:.2f}" if prof >= 0 else f"{prof:.2f}"
                r[27] = f"{hit[2]:.2f}"
    with open(rolling_store, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for k in sorted(all_rows):
            w.writerow(all_rows[k])
    write_xlsx(allfile, [h if not h[0].isdigit() else h for h in header], merged,
               widths=[11, 12, 7, 9, 6, 22, 6, 7, 16, 20, 8, 8, 8, 6, 6, 6, 6, 8, 8, 8, 8, 8, 8, 6, 8, 60, 8, 9, 9],
               sheet="all")
    if gd_bank != GRAIL_START_BANK:
        print(f"💰 ALL-DAYS LEDGER: £{GRAIL_START_BANK:.2f} → £{gd_bank:.2f} "
              f"(every top-three bet settled so far, Ladbrokes each-way 1/5 odds)")
    return [day, allfile]


# --------------------------------------------------------------------------
def build(dstr: str, refresh: bool) -> list[dict]:
    trn_result = scrape_rm_trainers(refresh, dstr)

    races = scrape_races_index(dstr, refresh)
    cards = []
    for race in races:
        try:
            card = fetch_racecard(dstr, race, refresh)
            runners = [r for r in card["runs"] if r["status"] == "RUNNER"]
            if len(runners) < MIN_RUNNERS:
                print(f"  · {race['time']} {race['meeting']}: "
                      f"{len(runners)} runners — under {MIN_RUNNERS}, skipped")
                continue
            cards.append(card)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {race['time']} {race['meeting']}: {exc}", file=sys.stderr)

    names = sorted({r["jockey"] for c in cards for r in c["runs"] if r["jockey"]})
    jky30 = fetch_hrn_jockeys(names, dstr, refresh)
    print(f"Jockey 30d records loaded: {len(jky30)}/{len(names)} "
          f"(missing jockeys score an empty box)")
    hrn = fetch_hrn_stats(dstr, refresh)
    course_jk, course_tr = (hrn.get("cj") or {}), (hrn.get("ct") or {})
    n_jk = sum(len(v) for v in course_jk.values())
    n_tr = sum(len(v) for v in course_tr.values())
    print(f"HRN stats: hot trainers {len(hrn.get('ht') or {})} · "
          f"hot jockeys {len(hrn.get('hj') or {})} · course jk {n_jk} · "
          f"course trn {n_tr} · "
          f"combos {sum(len(v) for v in (hrn.get('combos') or {}).values())}")

    for c in cards:
        c["scored"] = score_race(c, trn_result, jky30, course_jk, course_tr,
                                 hrn.get("ht") or {}, hrn.get("hj") or {},
                                 hrn.get("combos") or {})
        c["date"] = dstr

    cards.sort(key=lambda c: (c["time"], c["meeting"]))
    return cards


# --------------------------------------------------------------------------
# phone app (PWA repo)
# --------------------------------------------------------------------------
def app_day_payload(dstr: str, cards: list[dict]) -> dict:
    """Slim JSON for the app: one record per runner with the twelve boxes."""
    races_out = []
    for c in sorted(cards, key=lambda c: (c["time"], c["meeting"])):
        runners = []
        for rank, r in enumerate(c["scored"], start=1):
            runners.append({
                "rank": rank, "horse": r["horse"], "stars": r.get("stars"),
                "odds": r.get("odds"), "jockey": r.get("jockey"),
                "trainer": r.get("trainer"),
                "life": r.get("life_pts"), "trn": r.get("trn_pts"),
                "jky": r.get("jky_pts"), "fav": r.get("fav_pts"),
                "ht": r.get("ht_pts"), "hj": r.get("hj_pts"),
                "tjc": r.get("tjc_pts"),
                "cd": r.get("cd_pts"), "dist": r.get("dist_pts"),
                "crs": r.get("crs_pts"), "trnc": r.get("trnc_pts"),
                "cjkc": r.get("cjkc_pts"), "total": r.get("total"),
                "pos": r.get("pos"), "res": r.get("res"),
            })
        races_out.append({
            "time": c["time"], "meeting": c["meeting"],
            "name": c.get("race_name"), "class": c.get("class"),
            "distance": c.get("distance"), "going": c.get("going"),
            "settled": any(x.get("res") for x in c["scored"]),
            "runners": runners,
        })
    now = datetime.now(ZoneInfo("Europe/London"))
    return {
        "date": dstr,
        "updated": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "races": races_out,
    }


def run_git(*args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "-C", str(APP_DIR), *args],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return (p.stdout + p.stderr).strip()


def _seed_path(name: str) -> Path:
    return APP_DIR / "data" / name


def _write_seed(name: str, payload: dict) -> None:
    p = _seed_path(name)
    txt = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
    if not p.exists() or p.read_text(encoding="utf-8") != txt:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")


def seed_hrn_snapshot(data_dir: Path, dstr: str) -> None:
    """Publish the day's HRN /stats page into the repo — runner-hosted
    settles fall back to it when HRN's CDN 403-blocks datacenter IPs.
    (The resolved 30-day jockey seed is written by fetch_hrn_jockeys.)"""
    src = BASE_DIR / "cache" / f"gr-hrn-stats-{dstr}.html"
    if src.exists():
        dst = data_dir / f"hrn-stats-{dstr}.html"
        if not (dst.exists() and dst.stat().st_size == src.stat().st_size):
            dst.write_text(src.read_text(encoding="utf-8", errors="ignore"),
                           encoding="utf-8")
    for pat in ("hrn-stats-????-??-??.html", "hrn-jky30-????-??-??.json"):
        snaps = sorted(data_dir.glob(pat))
        for old in snaps[:-8]:
            old.unlink()


def publish_app(dstr: str, cards: list[dict], push: bool = True) -> None:
    """Write data/<date>.json + latest.json + index.json into the PWA repo,
    then commit (and push). Re-running with unchanged scoring is a no-op."""
    data_dir = APP_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    payload = app_day_payload(dstr, cards)

    grail_bank_replay(data_dir, dstr, cards)
    for rc0 in payload["races"]:
        for rr in rc0["runners"]:
            hit = _BANK.get(bank_key(dstr, rc0["time"], rc0["meeting"],
                                     rr["horse"]))
            if hit:
                rr["stake"], rr["bet_profit"], rr["bank_after"] = hit
    bs, be = _BANK_DAYS.get(dstr, (GRAIL_START_BANK, GRAIL_START_BANK))
    payload["bank"] = {"start": bs, "end": be}

    def sans_stamp(text: str) -> str:
        try:
            j = json.loads(text)
            j.pop("updated", None)          # compare scoring, not save-time
            return json.dumps(j, sort_keys=True, ensure_ascii=False)
        except Exception:
            return text

    day_path = data_dir / f"{dstr}.json"
    new_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    existing: dict | None = None
    if day_path.exists():
        try:
            existing = json.loads(day_path.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    shrunk = (existing is not None
              and len(existing.get("races") or [])
              > len(payload.get("races") or []))
    if shrunk:
        # thin index/fallback day (SL serves partial data at times) must
        # never replace a richer stored day
        print(f"  ⚠️ {dstr}: fetched only {len(payload.get('races') or [])} "
              f"races but {len(existing.get('races') or [])} are stored — "
              f"keeping the richer day file")
        new_text = day_path.read_text(encoding="utf-8")
    changed = (not shrunk and
               (not day_path.exists() or sans_stamp(day_path.read_text(
                   encoding="utf-8")) != sans_stamp(new_text)))
    if not shrunk:
        day_path.write_text(new_text, encoding="utf-8")

    days = sorted(p.stem for p in data_dir.glob("????-??-??.json"))
    latest = days[-1]
    latest_path = data_dir / "latest.json"
    src = json.loads((data_dir / f"{latest}.json").read_text(encoding="utf-8"))
    latest_path.write_text(
        json.dumps(src, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n_r = len(src.get("races") or [])
    print(f"App data: {latest_path.name} — {latest}, {n_r} races")

    seed_hrn_snapshot(data_dir, dstr)

    if not changed and run_git("status", "--porcelain", "--", "data") == "":
        print("No scoring change since last publish.")
        return

    (data_dir / "index.json").write_text(json.dumps(
        {"latest": latest, "days": list(reversed(days)),
         "updated": payload["updated"]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    if run_git("status", "--porcelain", "--", "data") == "":
        print("No change since last publish.")
        return

    run_git("add", "data")
    stamp = payload["updated"]
    run_git("-c", f"user.name={COMMIT_NAME}", "-c", f"user.email={COMMIT_EMAIL}",
            "commit", "-m", f"grail {dstr} {stamp[11:16]} (auto)")
    print("Committed app data.")

    if push:
        push_app_data()


def push_app_data() -> None:
    """Push the app repo, retrying GitHub's occasional TLS flakes."""
    last_exc: Exception | None = None
    for attempt in range(3):            # GitHub TLS flakes occasionally
        try:
            run_git("push")
            print("Pushed to GitHub — open the app on the phone and tap ⟳.")
            return
        except RuntimeError as exc:
            last_exc = exc
            time.sleep(3 * (attempt + 1))
    sys.stderr.write(
        "PUSH FAILED after 3 tries — run manually:\n"
        "  cd ~/projects/holy-grail && git push\n\n" + str(last_exc) + "\n")
    sys.exit(1)


def settle_only(dstr: str, push: bool = True) -> None:
    """Late-day mode: the morning publish is the sheet of record — stats are
    NEVER re-scored. This patches stored results only (res/pos off the fresh
    results card), marks any pick gone from the card NR, re-stamps the bank
    chain off the stored days, and pushes. A race not yet off stays exactly
    as stored — it settles whenever SL posts it, or not at all."""
    data_dir = APP_DIR / "data"
    day_path = data_dir / f"{dstr}.json"
    if not day_path.exists():
        print(f"settle {dstr}: no stored day file — nothing to settle")
        return
    payload = json.loads(day_path.read_text(encoding="utf-8"))

    try:
        index = scrape_races_index(dstr, True)
    except RuntimeError as exc:
        print(f"settle {dstr}: {exc} — stored day left untouched")
        return
    fresh: dict[str, tuple[dict, int | None]] = {}
    for race in index:
        try:
            card = fetch_racecard(dstr, race, True)
        except Exception:
            continue
        by_horse = {norm(rd["horse"]): rd for rd in card["runs"]}
        finished = any(isinstance(rd.get("pos"), int) and rd["pos"] >= 1
                       for rd in card["runs"])
        fresh[f"{norm(card['meeting'])}|{card['time']}"] = (
            by_horse, card.get("placed"), finished)

    def res_for(rd: dict, placed: int | None, finished: bool) -> str | None:
        if rd["status"] != "RUNNER":
            return "NR"
        pos = rd.get("pos")
        if pos in (None, 0):
            # result posted but this runner has no position → SL has no
            # result for it at all: void leg, stake returned
            return "NR" if finished else None
        p = int(pos)
        n = placed if placed else 3
        return "WIN" if p == 1 else ("PLACE" if p <= n else "LOSE")

    patched = 0
    for rc in payload.get("races") or []:
        hit = fresh.get(f"{norm(rc['meeting'])}|{rc['time']}")
        if not hit:
            continue
        by_horse, placed, finished = hit
        for rr in rc.get("runners") or []:
            if rr.get("res") in ("WIN", "PLACE", "LOSE"):
                continue                    # settled rows are frozen
            rd = by_horse.get(norm(rr["horse"]))
            if rd is None:
                new, newpos = "NR", None    # gone from the card → non-runner
            else:
                new = res_for(rd, placed, finished)
                if new is None:
                    continue                # still not off — no change
                newpos = rd.get("pos")
            if rr.get("res") == new:
                continue
            rr["res"], rr["pos"] = new, newpos
            patched += 1
        rc["settled"] = any(r.get("res") for r in (rc.get("runners") or []))

    if not patched:
        print(f"settle {dstr}: nothing new to settle")
        return
    payload["updated"] = datetime.now(
        ZoneInfo("Europe/London")).strftime("%Y-%m-%dT%H:%M:%S%z")
    day_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    # re-chain the whole ledger off the stored days (this day included)
    grail_bank_replay(data_dir, "9999-99-99", [])
    for rc in payload.get("races") or []:
        for rr in rc.get("runners") or []:
            hit = _BANK.get(bank_key(dstr, rc["time"], rc["meeting"],
                                     rr["horse"]))
            if hit:
                rr["stake"], rr["bet_profit"], rr["bank_after"] = hit
    bs, be = _BANK_DAYS.get(dstr, (GRAIL_START_BANK, GRAIL_START_BANK))
    payload["bank"] = {"start": bs, "end": be}
    day_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    days = sorted(p.stem for p in data_dir.glob("????-??-??.json"))
    latest = days[-1]
    src = json.loads((data_dir / f"{latest}.json").read_text(encoding="utf-8"))
    (data_dir / "latest.json").write_text(
        json.dumps(src, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (data_dir / "index.json").write_text(json.dumps(
        {"latest": latest, "days": list(reversed(days)),
         "updated": payload["updated"]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    print(f"settle {dstr}: {patched} result rows patched · "
          f"bank £{bs:.2f} → £{be:.2f}")
    if run_git("status", "--porcelain", "--", "data") == "":
        print("No data change — nothing to commit.")
        return
    run_git("add", "data")
    run_git("-c", f"user.name={COMMIT_NAME}", "-c", f"user.email={COMMIT_EMAIL}",
            "commit", "-m", f"grail {dstr} {payload['updated'][11:16]} (auto)")
    print("Committed app data.")
    if push:
        push_app_data()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score today's cards — The Holy Grail")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in London)")
    ap.add_argument("--refresh", action="store_true", help="ignore cached pages")
    ap.add_argument("--publish", action="store_true",
                    help="push the day's scoring to the phone app repo")
    ap.add_argument("--settle", action="store_true",
                    help="settle-only: patch stored results + bank, no re-scoring")
    ap.add_argument("--no-push", action="store_true",
                    help="with --publish/--settle: commit only, don't push")
    args = ap.parse_args(argv)

    dstr = args.date or london_today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dstr):
        ap.error("--date must be YYYY-MM-DD")

    if args.settle:
        if args.publish:
            ap.error("--publish and --settle are mutually exclusive")
        settle_only(dstr, push=not args.no_push)
        return 0

    cards = build(dstr, args.refresh)

    grail_bank_replay(APP_DIR / "data", dstr, cards)
    for c in cards:
        c["bank_start"], c["bank_end"] = _BANK_DAYS.get(
            dstr, (GRAIL_START_BANK, GRAIL_START_BANK))
        for rr in c["scored"]:
            hit = _BANK.get(bank_key(dstr, c["time"], c["meeting"], rr["horse"]))
            if hit:
                rr["stake"], rr["bet_profit"], rr["bank_after"] = hit

    print(f"\n=== 🏆 THE HOLY GRAIL — {dstr} ===")
    for c in cards:
        settled = sum(1 for r in c["scored"] if r["res"])
        print(f"\n{c['time']} {c['meeting']} ({len(c['scored'])} runners, "
              f"{c['distance']}, class {c['class']})"
              + (f" · {settled} settled" if settled else ""))
        for i, r in enumerate(c["scored"][:TOP3], start=1):
            marks = " ".join(
                f"{lbl}:{box(r[k])}" for k, lbl in BOXES if r.get(k) is not None
            ) or "no data"
            hit = _BANK.get(bank_key(dstr, c["time"], c["meeting"], r["horse"]))
            extra = ""
            if hit:
                sign = "+" if hit[1] >= 0 else ""
                extra = f" [{hit[0]:.2f} → {sign}{hit[1]:.2f}]"
            print(f"   {i}. {r['horse']:20} {r['odds'] or '':6} "
                  f"Σ {box(r['total']):>6}  ({marks}){extra}")

    bs, be = _BANK_DAYS.get(dstr, (GRAIL_START_BANK, GRAIL_START_BANK))
    if be != bs:
        print(f"   💰 BANK £{bs:.2f} → £{be:.2f} "
          f"(£10 start · 1% on each top three · Ladbrokes each-way 1/5 odds)")

    draft = DRAFT_DIR / dstr
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "scores.json").write_text(
        json.dumps(cards, indent=2, default=str, ensure_ascii=False) + "\n",
        encoding="utf-8")
    html = write_html(dstr, cards)
    xls = write_xlsx(dstr, cards)
    print(f"\nHTML: {html}")
    for x in xls:
        print(f"XLSX: {x}")

    if args.publish:
        publish_app(dstr, cards, push=not args.no_push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
