# The Holy Grail — phone PWA

Installable app version of the desktop scorer (`~/holy-grail/score_grail.py`).
Hosted free on GitHub Pages; the laptop pushes data after every scoring run.

## Install on the phone (one-time)

1. Open **https://jobsaboba.github.io/holy-grail/** in Chrome (Android) or Safari (iPhone).
2. **Add to Home screen** — Chrome: ⋮ menu; Safari: Share → "Add to Home Screen".
3. Fills the whole screen like a native app, keeps the last-loaded day offline.

## Daily flow

1. Laptop: `python3 ~/holy-grail/score_grail.py --publish` (morning; rerun as cards firm)
2. Evening rerun adds finishing positions; `--publish` pushes the settle.
3. Phone: tap **⟳** — always pulls fresh data straight from origin.

## What's on the phone

- Day chips (06/11, 07/11, 08/11 …) — swipe across saved days.
- Summary strip: top-3 outcome counts (WIN / PLACE / LOSE / pending).
- Per-race cards: runners sorted highest score first, gold rows = the top-3
  bets, nine point boxes under each horse (`·` = empty box / no data),
  result chip (WON n / st n / LOSE / NR) once settled.

## Files

| File | What it is |
|---|---|
| `index.html` | The whole app (offline-capable, logic local to the phone) |
| `sw.js` | Service worker — shell cache, data always fetched fresh from origin |
| `manifest.webmanifest` | Install metadata (name, icons, colours) |
| `icons/` | 192 + 512 px app icons |
| `data/latest.json` | Newest day's scores (mirror of the matching `data/<date>.json`) |
| `data/<date>.json` | One day: races, runners, point boxes, results |
| `data/index.json` | Day list for the chips (written on publish) |

Data comes from `score_grail.py --publish` on the laptop — nothing in the app
is editable; each day file is a snapshot of that day's scoring.
