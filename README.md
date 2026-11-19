# FlexiSpot price tracker

Checks a FlexiSpot UK product page, logs the price to `price_history.csv`, and
alerts you when something worth knowing changes:

- the price drops (any amount)
- the price reaches your `--target` (once, when it first crosses it)
- the item goes out of stock or comes back
- the promo banner changes

## Flags

| Flag | What it does | Default |
|---|---|---|
| `--url URL` | Product page to track | the Waffle recliner |
| `--target GBP` | Alert when the sale price is at or below this | none |
| `--every MINUTES` | Keep running, checking every N minutes (+ up to 60 s random jitter) | single check |
| `--history PATH` | CSV file for the price log | `price_history.csv` next to the script |
| `--no-notify` | Log only, send no alerts | alerts on |
| `--test-alert` | Send a test alert to every configured channel and exit | |
| `-h`, `--help` | Show help | |

## 1. Install

```bash
cd flexispot_price_tracker
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 -m pytest -q
```

## 2. Alerts

Open `tracker.py` and paste your values into the **ALERT SETTINGS** block at the top:

```python
TELEGRAM_BOT_TOKEN = "123456789:AAH..."
TELEGRAM_CHAT_ID = "987654321"
NTFY_TOPIC = ""
```

Environment variables with the same names override these.

**Telegram:** message **@BotFather**, send `/newbot`, copy the token. Send your new
bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and
copy `"chat":{"id": ...}`.

**ntfy (optional, no account):** install the ntfy app and subscribe to a hard-to-guess
topic name. Anyone who knows the name can read it.

Check it: `python3 tracker.py --test-alert`

## 3. Run every 30 minutes on your laptop

```bash
caffeinate -i python3 tracker.py --every 30 --target 380
```

`caffeinate -i` stops the Mac sleeping while it runs. Ctrl+C stops it. Failed
checks are logged and the loop carries on.

## 4. Or run it free in the cloud (GitHub Actions)

The workflow in `.github/workflows/price-tracker.yml` runs a check every 30 minutes
and commits `price_history.csv` back so each run can compare with the last.

1. Create a new GitHub repo on your **personal** account and push this folder's
   contents to its root (the `.github` folder included).
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (and/or `NTFY_TOPIC`).
   Leave the fields in `tracker.py` empty in this repo.
3. **Actions** tab → enable workflows → *FlexiSpot price tracker* → **Run workflow**
   to test it once.

Free minutes: a **public** repo is unlimited (secrets stay hidden, only the price
history is visible). A **private** repo has 2,000 min/month and each run uses about
2, so switch the cron to hourly (`0 * * * *`) there. GitHub can start scheduled
runs several minutes late, and pauses schedules on repos with no activity for
60 days (the history commits count as activity).
