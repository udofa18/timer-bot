"""
Telegram Countdown Timer Bot
=============================

Features
--------
- /timer  -> shows a display (00:00:00) with buttons: -1 min | Clear | +1 min, and Start
- Start begins a live countdown, editing the same message every second
- While running: a single toggle button (⏸ Pause <-> ▶️ Resume) plus Stop
- When the countdown hits 00:00:00, the bot sends a one-off "time's up" alert
  and then keeps counting UP as overtime (e.g. "+00:00:07 OVERTIME") --
  pause/resume still works during overtime.
- Works in private chats and groups. Any user in the chat can operate the
  buttons by default (see OWNER_ONLY_CONTROL below to lock it to the starter).

Setup
-----
1. Talk to @BotFather on Telegram, /newbot, get your token.
2. pip install -r requirements.txt   (see bottom comment for the one line needed)
3. export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
4. python telegram_timer_bot.py
"""

import logging
import os

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


def format_hms(total_seconds: int) -> str:
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


# ---- The class that owns pause/resume ------------------------------------

class TimerController:
    """
    Holds one chat's timer state and every state transition.
    `started` is True from the moment Start is pressed until Stop is pressed.
    `running` is True only while it should actually be ticking -- pausing
    just flips `running` to False without resetting anything, and resuming
    flips it back. This same flag governs both the countdown phase and the
    overtime phase, so pause/resume works in either.
    """

    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.message_id = None
        self.remaining = 0          # seconds left, while counting down
        self.overtime_seconds = 0   # seconds elapsed, once in overtime
        self.overtime = False
        self.started = False
        self.running = False

    # ---- editing the set time (only while idle, i.e. not started) ----

    def add_minute(self):
        if not self.started:
            self.remaining += STEP_SECONDS

    def subtract_minute(self):
        if not self.started:
            self.remaining = max(0, self.remaining - STEP_SECONDS)

    def clear(self):
        if not self.started:
            self.remaining = 0

    # ---- lifecycle ----

    def start(self) -> bool:
        if self.remaining <= 0:
            return False
        self.started = True
        self.running = True
        self.overtime = False
        self.overtime_seconds = 0
        return True

    def toggle_pause_resume(self):
        """The core ask: one button, flips running <-> paused."""
        if self.started:
            self.running = not self.running

    def stop(self):
        self.started = False
        self.running = False
        self.overtime = False
        self.overtime_seconds = 0
        self.remaining = 0

    def tick(self) -> bool:
        """
        Advance the clock by one second. Returns True exactly on the tick
        that crosses into overtime, so the caller knows to send the alert.
        """
        just_hit_zero = False
        if not self.overtime:
            self.remaining -= 1
            if self.remaining <= 0:
                self.remaining = 0
                self.overtime = True
                self.overtime_seconds = 0
                just_hit_zero = True
        else:
            self.overtime_seconds += 1
        return just_hit_zero

    # ---- rendering ----

    def render_text(self) -> str:
        if self.overtime:
            clock = format_hms(self.overtime_seconds)
            status = "▶️ Running" if self.running else "⏸ Paused"
            return f"⏰ *TIME'S UP* — {status}\nOvertime: `+{clock}`"
        clock = format_hms(self.remaining)
        if not self.started:
            status = "⏸ Set"
        else:
            status = "▶️ Running" if self.running else "⏸ Paused"
        return f"⏱ *Timer* — {status}\n\n`{clock}`"

    def build_keyboard(self) -> InlineKeyboardMarkup:
        if not self.started:
            rows = [
                [
                    InlineKeyboardButton("➖ 1 min", callback_data="timer:minus"),
                    InlineKeyboardButton("🗑 Clear", callback_data="timer:clear"),
                    InlineKeyboardButton("➕ 1 min", callback_data="timer:plus"),
                ],
                [InlineKeyboardButton("▶️ Start", callback_data="timer:start")],
            ]
        else:
            toggle_label = "⏸ Pause" if self.running else "▶️ Resume"
            rows = [
                [
                    InlineKeyboardButton(toggle_label, callback_data="timer:toggle"),
                    InlineKeyboardButton("⏹ Stop", callback_data="timer:stop"),
                ]
            ]
        return InlineKeyboardMarkup(rows)


# ---- Telegram plumbing ------------------------------------------------

async def safe_edit(context: ContextTypes.DEFAULT_TYPE, chat_id: int, timer: TimerController):
    """Edit the timer message, tolerating flood limits and 'not modified' errors."""
    if timer.message_id is None:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=timer.message_id,
            text=timer.render_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=timer.build_keyboard(),
        )
    except RetryAfter as e:
        logger.warning("Rate limited, retrying in %s s", e.retry_after)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Edit failed: %s", e)


def stop_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()


def ensure_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Make sure exactly one ticking job exists for this chat."""
    if not context.job_queue.get_jobs_by_name(str(chat_id)):
        context.job_queue.run_repeating(
            tick_job, interval=TICK_SECONDS, first=TICK_SECONDS, chat_id=chat_id, name=str(chat_id)
        )


# ---- Commands -------------------------------------------------------------

async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    timer = TimerController(owner_id=update.effective_user.id)
    context.chat_data["timer"] = timer

    msg = await update.effective_message.reply_text(
        timer.render_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=timer.build_keyboard(),
    )
    timer.message_id = msg.message_id


# ---- Ticking ----------------------------------------------------------

async def tick_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    timer = context.chat_data.get("timer")

    # The job keeps running even while paused (cheap no-op) so it's ready
    # the instant Resume is pressed, without re-registering a new job.
    if not timer or not timer.started:
        stop_job(context, chat_id)
        return

    if not timer.running:
        return  # paused -- do nothing this tick

    just_hit_zero = timer.tick()
    if just_hit_zero:
        await context.bot.send_message(chat_id=chat_id, text="⏰ Time's up! Now counting overtime…")

    await safe_edit(context, chat_id, timer)


# ---- Button handling ----------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    timer = context.chat_data.get("timer")

    if not timer:
        await query.answer("No active timer here — send /timer to start one.", show_alert=True)
        return

    if OWNER_ONLY_CONTROL and update.effective_user.id != timer.owner_id:
        await query.answer("Only the person who started this timer can control it.", show_alert=True)
        return

    action = query.data.split(":", 1)[1]

    if action == "plus":
        timer.add_minute()
    elif action == "minus":
        timer.subtract_minute()
    elif action == "clear":
        timer.clear()
    elif action == "start":
        if not timer.start():
            await query.answer("Set a time first with +1 min.", show_alert=True)
            return
        ensure_job(context, chat_id)
    elif action == "toggle":
        timer.toggle_pause_resume()
    elif action == "stop":
        timer.stop()
        stop_job(context, chat_id)

    await query.answer()
    await safe_edit(context, chat_id, timer)


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
