from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from gndu_catalog_checker import CatalogCheckResult, GNDUCatalogChecker


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("gndu-catalog-alert")


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STATE_FILE = Path(os.environ.get("CATALOG_STATE_FILE", "catalog_state.json"))


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using %s", name, default)
        return default


def env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(float(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using %s", name, default)
        return default


SCAN_INTERVAL_SECONDS = env_int("CATALOG_SCAN_INTERVAL_SECONDS", 3600, 900)
HEARTBEAT_INTERVAL_SECONDS = env_int("CATALOG_HEARTBEAT_INTERVAL_SECONDS", 3600, 3600)
REQUEST_DELAY_SECONDS = env_float("CATALOG_REQUEST_DELAY_SECONDS", 0.2, 0.0)

checker = GNDUCatalogChecker(request_delay_seconds=REQUEST_DELAY_SECONDS)
scan_lock = asyncio.Lock()


def empty_state() -> dict[str, Any]:
    return {
        "subscribers": [],
        "snapshot": None,
        "events": {},
        "delivered": {},
        "last_scan": None,
        "last_scan_summary": None,
        "last_scan_new_entries": 0,
        "last_heartbeat": None,
        "last_error": None,
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return empty_state()
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state = empty_state()
        state["subscribers"] = [int(chat_id) for chat_id in raw.get("subscribers", [])]
        state["snapshot"] = raw.get("snapshot")
        state["events"] = raw.get("events", {}) if isinstance(raw.get("events", {}), dict) else {}
        state["delivered"] = raw.get("delivered", {}) if isinstance(raw.get("delivered", {}), dict) else {}
        state["last_scan"] = raw.get("last_scan")
        state["last_scan_summary"] = raw.get("last_scan_summary")
        try:
            state["last_scan_new_entries"] = max(int(raw.get("last_scan_new_entries", 0)), 0)
        except (TypeError, ValueError):
            state["last_scan_new_entries"] = 0
        state["last_heartbeat"] = raw.get("last_heartbeat")
        state["last_error"] = raw.get("last_error")
        return state
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Could not read catalog state; starting fresh: %s", exc)
        return empty_state()


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_FILE)


def environment_chat_ids() -> set[int]:
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


def all_subscribers(state: dict[str, Any]) -> set[int]:
    return set(state.get("subscribers", [])) | environment_chat_ids()


def add_subscriber(chat_id: int) -> None:
    state = load_state()
    if chat_id not in state["subscribers"]:
        state["subscribers"].append(chat_id)
        save_state(state)


def remove_subscriber(chat_id: int) -> None:
    state = load_state()
    state["subscribers"] = [item for item in state["subscribers"] if item != chat_id]
    save_state(state)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def event_key(event: dict[str, Any]) -> str:
    if event["type"] == "new_course":
        return f"course:{event['course_code']}"
    semester_id = event.get("semester_value") or normalise(event.get("semester_text", ""))
    return f"semester:{event['course_code']}:{semester_id}"


def events_from_result(result: CatalogCheckResult) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for course in result.new_courses or []:
        event = {
            "type": "new_course",
            "course_code": course.get("code", ""),
            "course_text": course.get("text", ""),
            "semesters": course.get("semesters", []),
            "detected_at": result.checked_at_utc,
        }
        events[event_key(event)] = event
    for semester in result.new_semesters or []:
        event = {
            "type": "new_semester",
            "course_code": semester.get("course_code", ""),
            "course_text": semester.get("course_text", ""),
            "semester_text": semester.get("semester_text", ""),
            "semester_value": semester.get("semester_value", ""),
            "detected_at": result.checked_at_utc,
        }
        events[event_key(event)] = event
    return events


def event_line(event: dict[str, Any]) -> str:
    course = escape(str(event.get("course_text", "Unknown course")))
    code = escape(str(event.get("course_code", "")))
    if event.get("type") == "new_course":
        semesters = event.get("semesters", [])
        semester_names = [
            escape(str(semester.get("text", "")))
            for semester in semesters
            if semester.get("text")
        ]
        if semester_names:
            semester_details = "\n  <b>Semesters listed:</b> " + ", ".join(semester_names)
        else:
            semester_details = "\n  <b>Semesters listed:</b> none"
        return f"• <b>New class/course:</b> {course} <code>({code})</code>{semester_details}"
    semester = escape(str(event.get("semester_text", "New semester")))
    return f"• <b>New semester:</b> {course} <code>({code})</code> — {semester}"


def notification_chunks(events: list[dict[str, Any]]) -> list[tuple[list[str], list[str]]]:
    """Return message text and event keys in Telegram-safe chunks."""
    chunks: list[tuple[list[str], list[str]]] = []
    lines: list[str] = []
    keys: list[str] = []
    current_length = 0

    for event in events:
        key = event_key(event)
        line = event_line(event)
        if lines and current_length + len(line) + 1 > 3400:
            chunks.append((lines, keys))
            lines, keys, current_length = [], [], 0
        lines.append(line)
        keys.append(key)
        current_length += len(line) + 1

    if lines:
        chunks.append((lines, keys))
    return chunks


async def send_new_entry_alerts(application: Application, state: dict[str, Any]) -> None:
    events = state.get("events", {})
    if not events:
        return

    ordered_events = sorted(
        events.values(),
        key=lambda event: (event.get("detected_at", ""), event.get("type", ""), event.get("course_code", "")),
    )
    chunks = notification_chunks(ordered_events)
    for chat_id in sorted(all_subscribers(state)):
        delivered = set(str(key) for key in state.get("delivered", {}).get(str(chat_id), []))
        for lines, keys in chunks:
            unsent_keys = [key for key in keys if key not in delivered]
            if not unsent_keys:
                continue
            unsent_events = [events[key] for key in unsent_keys]
            text = (
                "<b>GNDU catalog update</b>\n\n"
                "GNDU added the following 2026 May CBGS New option(s):\n"
                + "\n".join(event_line(event) for event in unsent_events)
                + "\n\nThis means a new class/course or semester is now listed in the official result form.\n"
                "https://collegeadmissions.gndu.ac.in/studentArea/GNDUEXAMRESULT.aspx"
            )
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                delivered.update(unsent_keys)
                state.setdefault("delivered", {})[str(chat_id)] = sorted(delivered)
                save_state(state)
            except Exception as exc:
                logger.warning("Could not notify chat %s: %s", chat_id, exc)


def catalog_summary(snapshot: dict[str, Any] | None) -> tuple[int, int]:
    if not snapshot:
        return 0, 0
    courses = snapshot.get("courses", {})
    semester_count = sum(len(course.get("semesters", [])) for course in courses.values())
    return len(courses), semester_count


async def run_scan(application: Application) -> CatalogCheckResult:
    async with scan_lock:
        current_state = load_state()
        result = await asyncio.to_thread(checker.check, current_state.get("snapshot"))
        current_state["last_scan"] = result.checked_at_utc
        current_state["last_scan_summary"] = result.reason
        current_state["last_scan_new_entries"] = (
            len(result.new_courses or []) + len(result.new_semesters or [])
            if result.ok
            else 0
        )
        current_state["last_error"] = None if result.ok else result.reason

        if result.ok and result.snapshot is not None:
            if not result.baseline_created:
                for key, event in events_from_result(result).items():
                    current_state.setdefault("events", {}).setdefault(key, event)
            current_state["snapshot"] = result.snapshot

        save_state(current_state)
        logger.info("Catalog scan: ok=%s reason=%s", result.ok, result.reason)
        if result.ok and not result.baseline_created:
            await send_new_entry_alerts(application, current_state)
        return result


def heartbeat_message(state: dict[str, Any]) -> str:
    last_scan = state.get("last_scan") or "No completed scan yet"
    last_error = state.get("last_error")
    new_entries = int(state.get("last_scan_new_entries", 0) or 0)

    if last_error:
        status = (
            "The latest automatic scan could not be completed. The bot is still live "
            "and will retry automatically."
        )
    elif not state.get("last_scan"):
        status = "The first automatic catalog scan has not completed yet; it will run automatically."
    elif new_entries:
        status = (
            f"The latest automatic scan detected {new_entries} new class/semester option(s). "
            "The bot has sent the corresponding alert when delivery was possible."
        )
    else:
        status = (
            "The latest automatic scan found no new class or semester result option, "
            "so no new result entry is listed yet."
        )

    return (
        "<b>GNDU catalog bot is live</b>\n\n"
        "Automatic monitoring is running for 2026 May CBGS New.\n"
        f"{status}\n\n"
        f"Last automatic scan (UTC): {escape(str(last_scan))}\n"
        f"Next scan interval: {SCAN_INTERVAL_SECONDS} seconds\n"
        "This live-status message is sent automatically every hour."
    )


async def send_heartbeat(application: Application) -> None:
    current_state = load_state()
    message = heartbeat_message(current_state)
    delivered = 0
    for chat_id in sorted(all_subscribers(current_state)):
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            delivered += 1
        except Exception as exc:
            logger.warning("Could not send automatic heartbeat to chat %s: %s", chat_id, exc)

    current_state["last_heartbeat"] = utc_now()
    save_state(current_state)
    logger.info("Automatic hourly heartbeat attempted for %s subscriber chat(s)", delivered)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "You are subscribed to all-class GNDU updates. I will monitor 2026 May CBGS New and alert you when a new course/class or a new semester is added.\n\n"
        "The first scan creates a baseline, so existing entries are not reported as new.\n"
        "I will also send an automatic live-status message every hour.\n\n"
        "Commands:\n"
        "/scan — scan immediately\n"
        "/status — show scan status\n"
        "/unsubscribe — stop alerts"
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("You have been unsubscribed from new GNDU class and semester alerts.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    current = load_state()
    courses, semesters = catalog_summary(current.get("snapshot"))
    pending = len(current.get("events", {}))
    await update.message.reply_text(
        "Monitoring: GNDU 2026 May CBGS New (all courses/classes)\n"
        f"Last scan (UTC): {current.get('last_scan') or 'No scan completed yet.'}\n"
        f"Catalog currently contains: {courses} courses/classes and {semesters} semester options\n"
        f"Detected update events retained: {pending}\n"
        f"Scan interval: {SCAN_INTERVAL_SECONDS} seconds\n"
        f"Last automatic heartbeat (UTC): {current.get('last_heartbeat') or 'Not sent yet.'}\n"
        f"Latest scan summary: {current.get('last_scan_summary') or 'No scan completed yet.'}\n"
        f"Latest error: {current.get('last_error') or 'none'}"
    )


async def scan_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("Scanning all GNDU classes and their semester options. This may take a little time…")
    result = await run_scan(context.application)
    if not result.ok:
        await update.message.reply_text(f"The scan could not be completed. The bot will keep trying.\n\n{result.reason}")
    elif result.baseline_created:
        await update.message.reply_text("Baseline saved. Existing classes and semesters will not be reported; only future additions will trigger alerts.")
    elif result.new_courses or result.new_semesters:
        await update.message.reply_text(result.reason)
    else:
        await update.message.reply_text("No new class or semester was detected.")


async def periodic_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Automatic catalog scan starting")
    await run_scan(context.application)


async def periodic_heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Automatic hourly heartbeat starting")
    await send_heartbeat(context.application)


async def post_init(application: Application) -> None:
    if application.job_queue is None:
        logger.error("Job queue is unavailable; install python-telegram-bot[job-queue].")
        return
    application.job_queue.run_repeating(
        periodic_scan,
        interval=SCAN_INTERVAL_SECONDS,
        first=10,
        name="gndu-catalog-scan",
    )
    application.job_queue.run_repeating(
        periodic_heartbeat,
        interval=HEARTBEAT_INTERVAL_SECONDS,
        first=60,
        name="gndu-catalog-hourly-heartbeat",
    )
    logger.info(
        "Automatic jobs scheduled: catalog scan every %ss; heartbeat every %ss (first heartbeat in 60s)",
        SCAN_INTERVAL_SECONDS,
        HEARTBEAT_INTERVAL_SECONDS,
    )


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("scan", scan_now))
    return application


if __name__ == "__main__":
    app = build_application()
    logger.info(
        "Starting GNDU catalog alert bot; scan_interval=%ss heartbeat_interval=%ss",
        SCAN_INTERVAL_SECONDS,
        HEARTBEAT_INTERVAL_SECONDS,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)
