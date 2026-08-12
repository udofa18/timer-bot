"""
Telegram Countdown Timer Bot
=============================

Features
--------
- /timer  -> shows a display (00:00:00) with buttons: -1 min | Clear | +1 min, and Start
- Start begins a live countdown, editing the same message every second
- While running: Pause / Stop buttons replace the +/-/Clear/Start row
- When the countdown hits 00:00:00, the bot sends a one-off "time's up" alert
  and then keeps counting UP as overtime (e.g. "+00:00:07 OVERTIME")
- Works in private chats and groups. Any user in the chat can operate the
  buttons by default (see OWNER_ONLY_CONTROL below to lock it to the starter).

Setup
-----
1. Talk to @BotFather on Telegram, /newbot, get your token.
2. pip install -r requirements.txt   (see bottom comment for the one line needed)
3. export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
4. python telegram_timer_bot.py

Group usage
-----------
- Add the bot to the group as a normal member (no admin rights required just
  to send/edit its own messages).
- Bot privacy mode (BotFather -> /setprivacy) can stay ON/default, because
  this bot never needs to read ordinary group messages -- it only reacts to
  the /timer command and to button taps (callback queries), both of which
  reach the bot regardless of privacy mode.
"""

import logging
import os
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---- Config -----------------------------------------------------------

TICK_SECONDS = 1          # how often the display refreshes; use 2-3 in busy groups
STEP_SECONDS = 60         # what +1 min / -1 min adjusts by
OWNER_ONLY_CONTROL = False  # True -> only the person who ran /timer can press buttons

# ---- Helpers ------------------------------------------------------------

def format_hms(total_seconds: int) -> str:
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def default_state(owner_id: int) -> dict:
    return {
        "remaining": 0,       # seconds left on the clock (>=0 while not in overtime)
        "running": False,
        "overtime": False,
        "overtime_seconds": 0,
        "owner_id": owner_id,
        "message_id": None,
    }


def render_text(state: dict) -> str:
    if state["overtime"]:
        clock = format_hms(state["overtime_seconds"])
        return f"⏰ *TIME'S UP*\nOvertime: `+{clock}`"
    clock = format_hms(state["remaining"])
    status = "▶️ Running" if state["running"] else "⏸ Set"
    return f"⏱ *Timer* — {status}\n\n`{clock}`"


def build_keyboard(state: dict) -> InlineKeyboardMarkup:
    if state["running"] or state["overtime"]:
        rows = [
            [
                InlineKeyboardButton("⏸ Pause", callback_data="timer:pause"),
                InlineKeyboardButton("⏹ Stop", callback_data="timer:stop"),
            ]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("➖ 1 min", callback_data="timer:minus"),
                InlineKeyboardButton("🗑 Clear", callback_data="timer:clear"),
                InlineKeyboardButton("➕ 1 min", callback_data="timer:plus"),
            ],
            [InlineKeyboardButton("▶️ Start", callback_data="timer:start")],
        ]
    return InlineKeyboardMarkup(rows)


async def safe_edit(context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: dict):
    """Edit the timer message, tolerating flood limits and 'not modified' errors."""
    if state["message_id"] is None:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=state["message_id"],
            text=render_text(state),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_keyboard(state),
        )
    except RetryAfter as e:
        # Telegram is rate-limiting us; back off and try once more shortly.
        logger.warning("Rate limited, retrying in %s s", e.retry_after)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Edit failed: %s", e)


def stop_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()


# ---- Commands -------------------------------------------------------------

async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = default_state(owner_id=update.effective_user.id)
    context.chat_data["timer"] = state

    msg = await update.effective_message.reply_text(
        render_text(state),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_keyboard(state),
    )
    state["message_id"] = msg.message_id


# ---- Ticking ----------------------------------------------------------

async def tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = context.chat_data.get("timer")
    if not state or not state["running"]:
        stop_job(context, chat_id)
        return

    if not state["overtime"]:
        state["remaining"] -= 1
        if state["remaining"] <= 0:
            state["remaining"] = 0
            state["overtime"] = True
            state["overtime_seconds"] = 0
            # one-off alert, separate from the live display message
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏰ Time's up! Now counting overtime…",
            )
    else:
        state["overtime_seconds"] += 1

    await safe_edit(context, chat_id, state)


# ---- Button handling ----------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    state = context.chat_data.get("timer")

    if not state:
        await query.answer("No active timer here — send /timer to start one.", show_alert=True)
        return

    if OWNER_ONLY_CONTROL and update.effective_user.id != state["owner_id"]:
        await query.answer("Only the person who started this timer can control it.", show_alert=True)
        return

    action = query.data.split(":", 1)[1]

    if action == "plus":
        state["remaining"] += STEP_SECONDS
    elif action == "minus":
        state["remaining"] = max(0, state["remaining"] - STEP_SECONDS)
    elif action == "clear":
        state["remaining"] = 0
        state["overtime"] = False
        state["overtime_seconds"] = 0
    elif action == "start":
        if state["remaining"] <= 0:
            await query.answer("Set a time first with +1 min.", show_alert=True)
            return
        state["running"] = True
        state["overtime"] = False
        stop_job(context, chat_id)  # avoid duplicate jobs
        context.job_queue.run_repeating(
            tick, interval=TICK_SECONDS, first=TICK_SECONDS, chat_id=chat_id, name=str(chat_id)
        )
    elif action == "pause":
        state["running"] = False
        stop_job(context, chat_id)
    elif action == "stop":
        state["running"] = False
        state["overtime"] = False
        state["overtime_seconds"] = 0
        state["remaining"] = 0
        stop_job(context, chat_id)

    await query.answer()
    await safe_edit(context, chat_id, state)


# ---- Entry point --------------------------------------------------------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the TELEGRAM_BOT_TOKEN environment variable first.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("timer", cmd_timer))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^timer:"))

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# requirements.txt should contain:
#   python-telegram-bot[job-queue]==21.6
