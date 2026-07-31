# Split Sheet — hosted

Your training dashboard, on a web address you can open from your phone. Oura and
Strava connect once and then it syncs itself. Garmin gets there via Strava:
Garmin Connect already auto-uploads every run to Strava, and unlike Garmin,
Strava lets anyone use its API.

Nothing here costs money. You will make three free accounts and copy some values
between browser tabs. Set aside twenty minutes and do it in order — each step
produces something the next one needs.

---

## Step 1 — Put the code on GitHub

1. Make a free account at [github.com](https://github.com) if you don't have one.
2. Click **New repository**. Name it `splitsheet`. Choose **Private**. Create it.
3. On the empty repo page, click **uploading an existing file**.
4. Drag in every file from this folder. Commit.

That's it — you never have to touch GitHub again except to re-upload
`plan.json` when you add a new training block.

---

## Step 2 — Make a database

Your logins have to survive restarts, and hosted servers wipe their disk every
time they restart. So the tokens live in a database.

1. Sign up at [neon.com](https://neon.com) — free, no card.
2. Create a project. Any name, any region.
3. On the dashboard, find **Connection string** and copy it. It looks like
   `postgresql://user:password@ep-something.neon.tech/neondb?sslmode=require`.
4. Paste it somewhere for a minute. This is your `DATABASE_URL`.

Neon's free tier doesn't expire. Render's free database does, after 30 days,
which is why this uses Neon instead.

---

## Step 3 — Deploy

1. Sign up at [render.com](https://render.com) — free, no card.
2. **New** → **Web Service** → connect your GitHub → pick `splitsheet`.
3. Render reads `render.yaml` and fills in the settings. Choose the **Free** plan.
4. Before clicking create, open **Environment** and add:

   | Key | Value |
   |---|---|
   | `APP_PASSWORD` | any password you'll remember — this is what stops strangers reading your data |
   | `DATABASE_URL` | the Neon string from step 2 |

5. Create it. Wait for the build — a couple of minutes.
6. Render gives you a URL like `https://splitsheet-xxxx.onrender.com`. Copy it.
7. Go back to **Environment**, add `BASE_URL` set to that exact URL (no trailing
   slash), and save. It redeploys itself.

Open the URL. You should get a password box. Sign in — the dashboard loads with
your July data and both services showing as not connected. That's expected.

---

## Step 4 — Connect Strava

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api).
2. Create an application. For **Authorization Callback Domain**, enter just the
   domain — `splitsheet-xxxx.onrender.com`, with no `https://` and no path.
3. Copy the **Client ID** and **Client Secret**.
4. In Render → Environment, add `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`.
   Save and let it redeploy.
5. Open your app, scroll to the bottom, click **Connect Strava**, approve.

Runs appear within a minute. Every future run flows Garmin → Strava → here on
its own.

---

## Step 5 — Connect Oura

1. Go to [cloud.ouraring.com](https://cloud.ouraring.com) → **Developer** →
   create a new application.
2. Redirect URI — the full URL this time:
   `https://splitsheet-xxxx.onrender.com/oauth/oura`
3. Copy the **Client ID** and **Client Secret** into Render as
   `OURA_CLIENT_ID` and `OURA_CLIENT_SECRET`. Save, let it redeploy.
4. Open your app, click **Connect Oura**, approve.

Last night's sleep is now there by morning. No export request, no waiting.

---

## Step 6 — Connect the Coach (optional)

A chat tab that reads your actual numbers — today's session, last night's
sleep, HRV, load ratio — and gives perspective on whether to trust the session
as written, what to do about an ache, or what to eat today. It never edits the
plan; it only advises. Skip this step and the rest of the app works exactly
the same, just without that tab active.

1. Go to [console.anthropic.com](https://console.anthropic.com), sign up.
2. Add a small amount of credit — a few dollars is plenty. This chat is cheap:
   a typical exchange costs a fraction of a cent, so the whole season should
   come to cents or a few dollars, not more. Check
   [current pricing](https://docs.claude.com/en/docs/about-claude/pricing)
   before relying on that number.
3. Go to **API Keys** → **Create Key**. Copy it — it's shown once.
4. In Render → **Environment**, add `ANTHROPIC_API_KEY` with that value. Save,
   let it redeploy.
5. Open your app. The Coach tab now has an input box instead of "not connected."

---

## Living with it

**Add it to your phone home screen.** Open the URL in Safari or Chrome, share
menu, Add to Home Screen. It behaves like an app from then on.

**It syncs itself.** Opening the app triggers a sync if the data is more than
90 minutes old. There's also a **Sync now** button at the bottom.

**First open of the day is slow.** Render's free tier puts the app to sleep
after 15 minutes idle, and waking takes about a minute. Everything after that is
instant. Paying $7/month removes it — worth it in November, unnecessary now.

**Keep the plan current.** `plan.json` runs to 23 August. Add the next Final
Surge block in the same shape, re-upload it to GitHub, and Render redeploys
automatically:

```json
"2026-08-24": {
  "mi": 18,
  "hard": 1,
  "items": [
    {"tag": "Session", "name": "Double Threshold: 1K / 400s", "det": "", "vol": "18 mi"},
    {"tag": "Strength", "name": "Weight Room", "det": "", "vol": ""}
  ]
}
```

`"hard": 1` puts the red dot on the day. `"rest": 1` greys it out.
`planned_miles.json` holds weekly targets, keyed by the Monday.

---

## If something breaks

**"Not synced yet"** — one of the services isn't connected. Check the bottom of
the dashboard, and check the keys are in Render's Environment.

**Connect button says keys are missing** — the client ID or secret didn't save,
or Render hasn't finished redeploying. Environment changes take about a minute.

**"That link expired"** — the app restarted mid-sign-in. Just click Connect again.

**Strava connects but no runs** — check the callback domain is the bare domain
with no `https://`. That one catches almost everyone.

**Coach says "not connected"** — `ANTHROPIC_API_KEY` isn't set, or Render hasn't
redeployed yet. It never breaks anything else; the other four tabs don't need it.

**Coach replies with an error** — press it again, it's usually transient. If it
keeps happening, the key itself is probably wrong or out of credit — check
console.anthropic.com.

**Everything looks empty after a while** — check the Neon project is still
active. If the database is unreachable the app still runs but forgets its logins,
and you'd reconnect both services.

Press **Sync now** and read the error if one appears — it names which service
failed and why.

---

## What's stored, and where

| Where | What |
|---|---|
| Neon | Your Oura and Strava tokens, the merged dataset, and your Coach conversation. |
| Render environment | Your client secrets and app password. Never in the code. |
| The repo | Code and your training plan. No credentials — `.gitignore` covers it. |

The app is password-protected and sends a no-referrer header so your URL doesn't
leak. It never sends your data anywhere except back to your own browser — and,
for the Coach tab specifically, to Anthropic's API, the same as any other app
built on it.
