from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from gndu_checker import CheckResult, GNDUResultChecker


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("gndu-result-alert")


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using %s", name, default)
        return default


CHECK_INTERVAL_SECONDS = env_int("CHECK_INTERVAL_SECONDS", 900, 300)
HEARTBEAT_INTERVAL_SECONDS = env_int("HEARTBEAT_INTERVAL_SECONDS", 3600, 3600)

checker = GNDUResultChecker()
check_lock = asyncio.Lock()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"subscribers": [], "notified": False, "last_check": None, "last_heartbeat": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "subscribers": [int(chat_id) for chat_id in data.get("subscribers", [])],
            "notified": bool(data.get("notified", False)),
            "last_check": data.get("last_check"),
            "last_heartbeat": data.get("last_heartbeat"),
        }
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Could not read state file; starting fresh: %s", exc)
        return {"subscribers": [], "notified": False, "last_check": None, "last_heartbeat": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def state() -> dict[str, Any]:
    # Loaded on demand so a state file edited while debugging is picked up.
    return load_state()


def add_subscriber(chat_id: int) -> dict[str, Any]:
    current = state()
    if chat_id not in current["subscribers"]:
        current["subscribers"].append(chat_id)
        save_state(current)
    return current


def remove_subscriber(chat_id: int) -> dict[str, Any]:
    current = state()
    current["subscribers"] = [item for item in current["subscribers"] if item != chat_id]
    save_state(current)
    return current


def selected_environment_subscribers() -> set[int]:
    raw = os.getenv("TELEGRAM_CHAT_IDS", "")
    result: set[int] = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                result.add(int(value))
            except ValueError:
                logger.warning("Ignoring invalid TELEGRAM_CHAT_IDS value: %r", value)
    return result


def all_subscribers() -> set[int]:
    return set(state()["subscribers"]) | selected_environment_subscribers()


def available_message(semester_text: str | None) -> str:
    semester = semester_text or "Bachelor of Commerce, Semester-IV"
    return (
        "<b>GNDU result update</b>\n\n"
        "The official GNDU result form now lists the target result option:\n"
        f"<b>{semester}</b>\n\n"
        "Open the official page and enter your roll number to view your marks:\n"
        "https://collegeadmissions.gndu.ac.in/studentArea/GNDUEXAMRESULT.aspx"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "You are subscribed. I will check the official GNDU result form automatically and alert you when B.Com 2026 May Semester-IV appears.\n\n"
        "Commands:\n"
        "/check — check immediately\n"
        "/status — show the last check\n"
        "/unsubscribe — stop alerts"
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("You have been unsubscribed from GNDU result alerts.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    current = state()
    last = current.get("last_check") or "No check completed yet."
    notified = "yes" if current.get("notified") else "no"
    await update.message.reply_text(
        f"Monitoring: GNDU 2026 May B.Com Semester-IV\n"
        f"Last check (UTC): {last}\n"
        f"Availability alert already sent: {notified}\n"
        f"Result polling interval: {CHECK_INTERVAL_SECONDS} seconds\n"
        f"Hourly live-status heartbeat: every {HEARTBEAT_INTERVAL_SECONDS} seconds\n"
        f"Last heartbeat (UTC): {current.get('last_heartbeat') or 'Not sent yet.'}"
    )


async def run_check(application: Application, *, force_alert: bool = False) -> CheckResult:
    async with check_lock:
        result = await asyncio.to_thread(checker.check)
        current = state()
        current["last_check"] = result.checked_at_utc
        save_state(current)
        logger.info("GNDU check: available=%s reason=%s", result.available, result.reason)

        if not result.available:
            return result
        if current.get("notified") and not force_alert:
            return result

        message = available_message(result.semester_text)
        delivered = False
        for chat_id in sorted(all_subscribers()):
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                delivered = True
            except Exception as exc:
                logger.warning("Could not notify chat %s: %s", chat_id, exc)

        # Mark only after a successful delivery; if there are no subscribers, a later /start can receive it.
        if delivered:
            current["notified"] = True
            save_state(current)

        return result


def heartbeat_message(result: CheckResult) -> str:
    if result.reason.startswith(("GNDU request failed", "Checker error")):
        status = "The latest GNDU check could not be confirmed because the website or network was temporarily unavailable."
    else:
        status = "The latest check did not find B.Com Semester-IV, so the result is not published/listed yet."
    return (
        "<b>GNDU bot heartbeat</b>\n\n"
        "The bot is live and monitoring the official result page.\n"
        f"{status}\n\n"
        f"Last check (UTC): {result.checked_at_utc}\n"
        "The bot will check again and send the result alert when Semester-IV appears."
    )


async def hourly_heartbeat(application: Application) -> None:
    result = await run_check(application)
    current = state()
    current["last_heartbeat"] = result.checked_at_utc
    save_state(current)

    # Once Semester-IV is listed, the one-time result alert is the useful notification;
    # do not continue sending messages that say the result is unpublished.
    if result.available:
        logger.info("Hourly heartbeat suppressed because Semester-IV is now available.")
        return

    message = heartbeat_message(result)
    for chat_id in sorted(all_subscribers()):
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Could not send hourly heartbeat to chat %s: %s", chat_id, exc)


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("Checking the official GNDU result form now…")
    await run_check(context.application, force_alert=False)
    current = state()
    if current.get("notified"):
        await update.message.reply_text("The target semester is listed, and the alert has been sent.")
    else:
        await update.message.reply_text("Semester-IV is not listed yet, or GNDU is temporarily unavailable. I will keep monitoring.")


async def periodic_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_check(context.application)


async def periodic_heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    await hourly_heartbeat(context.application)


async def post_init(application: Application) -> None:
    # The first automatic check happens shortly after startup, then at the configured interval.
    if application.job_queue is None:
        logger.error("Job queue is unavailable; install python-telegram-bot[job-queue].")
        return
    application.job_queue.run_repeating(
        periodic_check,
        interval=CHECK_INTERVAL_SECONDS,
        first=10,
        name="gndu-result-check",
    )
    application.job_queue.run_repeating(
        periodic_heartbeat,
        interval=HEARTBEAT_INTERVAL_SECONDS,
        first=HEARTBEAT_INTERVAL_SECONDS,
        name="gndu-hourly-heartbeat",
    )


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("check", check_now))
    return application


if __name__ == "__main__":
    app = build_application()
    logger.info(
        "Starting GNDU result alert bot; result_check_interval=%ss heartbeat_interval=%ss",
        CHECK_INTERVAL_SECONDS,
        HEARTBEAT_INTERVAL_SECONDS,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)
