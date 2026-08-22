# GNDU Result Alert Bots

This project includes two Telegram-monitoring modes for the official [GNDU examination-result page](https://collegeadmissions.gndu.ac.in/studentArea/GNDUEXAMRESULT.aspx). The original mode watches B.Com Semester-IV, while the second mode watches every course and semester under 2026 May CBGS New.

## Original mode: B.Com Semester-IV alert

The original mode checks this exact selection:

| Field | Value |
| --- | --- |
| Year | 2026 |
| Month | May |
| Course type | CBGS New (College Courses) |
| Course | Bachelor of Commerce (1211) |
| Target semester | Semester-IV |

The bot checks the public form without asking for or storing a roll number. It sends a Telegram alert when the target semester becomes available in the official form. It does not claim that marks are accessible for every student; open the official page and enter your own roll number after the alert.

While Semester-IV is not listed, the original bot sends a live-status heartbeat every hour saying that the bot is running and the result is not published/listed yet. Heartbeats stop after Semester-IV appears.

## Second mode: any new class or semester

The second script is `catalog_bot.py`. It monitors **2026 → May → CBGS New (College Courses)**, reads all available course/class options, and reads the semester options under every course.

The first scan creates a baseline. Existing entries in that first scan are not reported. On later scans, the bot sends an alert only when it detects either a course/class code that was not in the previous snapshot or a semester option newly added under an already-known course.

Therefore, a newly listed B.Com Semester-IV, a newly listed B.A. class, or a new semester under any previously listed course can trigger an alert. The message identifies the course name and code; for a new course it also lists the exact semesters currently shown under that course, and for a new semester it identifies the exact semester name. This monitor does not submit roll numbers or retrieve student marks.

A full catalog scan can take longer than the original bot because GNDU requires one server postback per course. The script includes a small delay between requests and uses a default one-hour scan interval. The minimum scan interval is 15 minutes.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | Original B.Com Semester-IV monitor, hourly heartbeat, subscribers, and one-time result alert |
| `gndu_checker.py` | Checker for the original B.Com Semester-IV mode |
| `catalog_bot.py` | New-course/new-semester Telegram monitor |
| `gndu_catalog_checker.py` | Collects every CBGS New course and its semester options |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway start command for the original mode |
| `.env.example` | Environment-variable template for both modes |
| `.gitignore` | Keeps tokens, state files, caches, and virtual environments out of Git |

## Telegram setup

Open Telegram, message `@BotFather`, use `/newbot`, and copy the token it provides. Never put the token into source code or commit it to GitHub.

If you run both modes as separate Railway services, create **two different Telegram bots and two different BotFather tokens**. Telegram polling allows only one active receiver per bot token, so running `bot.py` and `catalog_bot.py` simultaneously with the same token can cause update conflicts. If you only want the second mode, one token is enough for `catalog_bot.py` alone.

For either bot, send `/start` after deployment. The original bot supports `/check`, `/status`, and `/unsubscribe`. The catalog bot supports `/scan`, `/status`, and `/unsubscribe`.

## Railway deployment

Create a Railway project from your GitHub repository. For the original mode, Railway can use the included `Procfile` command:

```text
worker: python bot.py
```

Set the required variable in Railway:

```text
TELEGRAM_BOT_TOKEN=your_token_from_BotFather
```

For the second mode, create another Railway service from the same repository and set its start command to:

```text
python catalog_bot.py
```

Use the second bot’s token if both modes are running.

Optional variables for the original mode are:

```text
CHECK_INTERVAL_SECONDS=900
HEARTBEAT_INTERVAL_SECONDS=3600
TELEGRAM_CHAT_IDS=123456789
STATE_FILE=state.json
```

Optional variables for the catalog mode are:

```text
CATALOG_SCAN_INTERVAL_SECONDS=3600
CATALOG_REQUEST_DELAY_SECONDS=0.2
TELEGRAM_CHAT_IDS=123456789
CATALOG_STATE_FILE=catalog_state.json
```

The original result check is clamped to at least five minutes. The original heartbeat is clamped to at least one hour. The catalog scan is clamped to at least 15 minutes. These defaults keep requests and resource usage light.

Deploy the service as an **always-running worker**, not as a Railway Cron Job. The Telegram bot uses long polling and must remain running. After deployment, inspect the logs for `Starting GNDU` and send `/start` to the correct Telegram bot.

## State persistence

The original bot writes `state.json`. The catalog bot writes `catalog_state.json`. These files store subscribers, snapshots, detected events, and notification history. Without persistent storage, a service recreation or redeploy may reset the files.

If a Railway Volume is available, mount it at `/data` and set the corresponding variable:

```text
STATE_FILE=/data/state.json
CATALOG_STATE_FILE=/data/catalog_state.json
```

You can also set `TELEGRAM_CHAT_IDS` so the bot knows the recipient even if its local state file is reset. It accepts comma-separated chat IDs.

## Local test

Install dependencies and test the checkers without a Telegram token:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python gndu_checker.py
python gndu_catalog_checker.py
```

Run the original bot:

```bash
export TELEGRAM_BOT_TOKEN='your_original_bot_token'
python bot.py
```

Run the catalog bot instead:

```bash
export TELEGRAM_BOT_TOKEN='your_catalog_bot_token'
python catalog_bot.py
```

## Important behavior

The GNDU page is an ASP.NET Web Forms application. The checkers reproduce the required dropdown postbacks. The catalog checker compares course codes and semester values, so it does not repeatedly alert for entries already known.

Temporary network errors or page changes are treated as failed checks. The processes keep running and retry later rather than sending a false “new result” alert.

### References

[1]: https://collegeadmissions.gndu.ac.in/studentArea/GNDUEXAMRESULT.aspx "Guru Nanak Dev University examination-result page"
