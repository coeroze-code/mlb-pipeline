# MLB Pipeline - Setup Guide

This turns your local pipeline into a website you can run from your phone,
plus a daily automatic refresh. Two free services do the work:

- **GitHub** - hosts the code, and (via GitHub Actions) runs the pipeline
  automatically every day on a schedule.
- **Streamlit Community Cloud** - hosts the actual webpage you'll open on
  your phone, with a "Run Pipeline Now" button and a results table.

Total cost: $0/month, assuming you stay on free tiers (you will, at this scale).

---

## 1. Create the GitHub repo

1. Go to github.com, sign in (or create an account), click **New repository**.
2. Name it something like `mlb-pipeline`. Set it to **Private** (recommended,
   since your odds-api usage and betting workflow are personal).
3. Upload every file in this folder to the repo, **preserving the folder
   structure** - the `.github/workflows/daily_run.yml` file specifically
   needs to stay at that exact path or the scheduled run won't be picked up.
   Easiest way: on the repo page, click **Add file → Upload files**, drag in
   everything including the `.github` folder.
4. **Also upload your `MLB_-_Stats.csv`** (the batter season stats export) -
   it's not included in this folder since it's your own data file, but
   `mlb_pipeline.py` needs it to exist in the repo root. Whenever you refresh
   your season stats locally, re-upload it here the same way (Add file →
   Upload files → overwrite).

## 2. Add your odds-api keys as a GitHub secret

1. In the repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `ODDS_API_KEYS`
4. Value: all 7 of your keys, comma-separated, no spaces, e.g.:
   `key1,key2,key3,key4,key5,key6,key7`
5. Save.

This is what `daily_run.yml` reads from when it runs your scheduled job -
the actual key values never appear in the code or the repo itself.

## 3. Check/adjust the daily schedule

Open `.github/workflows/daily_run.yml`. The line:
```
- cron: "0 13 * * *"
```
runs the pipeline at 13:00 UTC, which is 9:00 AM Eastern during daylight
saving (8:00 AM the rest of the year - GitHub's cron is always UTC and
doesn't auto-adjust). Change the hour if you want it earlier/later, then
commit the change.

You can also trigger a scheduled-style run manually anytime from GitHub's
website or mobile app: go to **Actions → Daily MLB Pipeline Run → Run workflow**.

## 4. Deploy the Streamlit app

1. Go to **share.streamlit.io** and sign in with your GitHub account.
2. Click **New app**, pick your `mlb-pipeline` repo, branch `main`, and set
   the main file path to `streamlit_app.py`.
3. Before clicking Deploy, click **Advanced settings** and add a secret:
   ```
   ODDS_API_KEYS = "key1,key2,key3,key4,key5,key6,key7"
   ```
   (same keys as step 2, just in Streamlit's secrets box instead of GitHub's)
4. Click **Deploy**. You'll get a URL like `https://your-app-name.streamlit.app`.

## 5. Add it to your phone's home screen

Open that URL on your phone in Safari/Chrome, then use "Add to Home Screen"
(share menu on iOS, or the browser menu on Android). It'll behave like a
regular app icon from then on.

---

## How it works day to day

- **Every morning at your scheduled time**, GitHub Actions runs the full
  pipeline and commits fresh CSVs to the repo. Streamlit Cloud notices the
  repo changed and automatically reloads with the new data - you don't have
  to do anything.
- **Anytime you want fresher data**, open the app and tap **Run Pipeline
  Now**. This runs immediately in the app itself and updates the table you're
  looking at right away. (This immediate run doesn't get pushed back to the
  GitHub repo - it's a live, in-session refresh. If the app later restarts,
  it'll fall back to whatever GitHub Actions last committed. Let me know if
  you'd rather manual runs also persist back to GitHub - it's a small addition.)
- The free Streamlit tier **sleeps the app after a period of no visits** -
  the first load after a while asleep can take 30-60 seconds to wake back up.
  This is normal, not a bug.

## The one thing to verify first

`mlb_pipeline.py` hits DraftKings' sportsbook API directly. That's the one
part of this whole setup that *might* behave differently from a cloud IP
address than from your home connection (sportsbooks are more likely to
block/rate-limit known datacenter IPs). After deploying, tap **Run Pipeline
Now** and check whether `RBI.csv`/`TB.csv`-derived rows actually show up in
the Batters table. If they come back empty, message me - the fix is a small
hybrid tweak (keep just that one fetch running locally on a schedule,
pushing results to the same repo) rather than a rebuild.
