# Kalshi ↔ Polymarket US arb scanner

Every 5 minutes, GitHub's free servers pull prices from Kalshi and Polymarket US, match up markets that look like the same event, and check whether buying opposite sides on the two platforms costs less than $1 after fees. New opportunities are pushed to your phone through the free **ntfy** app.

Setup happens entirely in a web browser, so you don't install anything on your laptop. Cost: $0.

## Setup (about 15 minutes)

**1. Phone: install ntfy.** Get "ntfy" from the App Store or Google Play. Tap **+**, subscribe to a topic, and make up a long random name, for example `max-arb-7f3k9q2x`. Anyone who knows the topic name can read your alerts, so treat it like a password.

**2. GitHub: create a free account** at github.com.

**3. Create a repository.** Click **+ → New repository**, name it `arb-scanner`, and set it to **Public**. Public repos get unlimited free Actions minutes; private ones get 2,000/month, and a 5-minute schedule uses more than that. Your topic name stays hidden in step 5, so nothing sensitive is exposed.

**4. Upload the files.** Click **uploading an existing file** and drag in `scanner.py`, `requirements.txt`, and `README.md`. Commit. The workflow file sits in a hidden folder, so create it by hand: **Add file → Create new file**, type the name `.github/workflows/scan.yml` (the slashes create the folders), paste in the contents of `scan.yml`, and commit.

**5. Add your topic as a secret.** Go to **Settings → Secrets and variables → Actions → New repository secret**. Name: `NTFY_TOPIC`. Value: your topic name from step 1.

**6. Test it.** Go to the **Actions** tab → **Arb scan** → **Run workflow**, check "Only send a test notification", and run it. Your phone should buzz within a minute.

**7. Run a real scan.** Click **Run workflow** again with the box unchecked. Open the run → **scan** → **Scan** to see the log. It lists how many markets it pulled, how many pairs it matched, and the 10 pairs closest to an arb. After this, it runs automatically every 5 minutes.

## Tuning (edit `.github/workflows/scan.yml` in the browser)

| Setting | Default | Meaning |
|---|---|---|
| `MIN_EDGE` | 0.01 | Alert when net profit is at least 1¢ per $1 contract, after fees |
| `MAX_EDGE` | 0.08 | Anything bigger is almost always two *different* markets, so it's skipped |
| `MIN_MATCH_SCORE` | 85 | Title similarity (0–100) needed to treat two markets as the same |
| `MAX_DAYS_APART` | 3 | Max gap between the two markets' close dates |

## Things to know

- **Every alert is a lead, not a sure thing.** Matching goes by titles, so always open both markets and confirm that the resolution rules, settlement source, and handling of overtime, postponements, and cancellations are identical.
- **Prices are top-of-book.** The scanner checks Kalshi's YES-ask size but can't see depth on the other legs. A big order may fill at worse prices.
- **Fees are modeled as taker fees:** Kalshi ≈ 0.07·p·(1−p), rounded up to the cent per order; Polymarket US = 0.0695·p·(1−p). Resting limit orders (maker) cost less or earn a rebate.
- **Polymarket NO price** is computed as 1 − best YES bid, which is the standard way binary order books work. Verify it against the app the first time you get an alert.
- **Scheduled runs pause** if the repo has no activity for 60 days. GitHub emails you first; any commit restarts them.
- GitHub sometimes delays scheduled runs by several minutes when it's busy. This scanner is built for slower mispricings, not split-second ones.
