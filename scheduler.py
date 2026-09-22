import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    Defaults,
)


# ============================================================
# SOZLAMALAR
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7320261679

TIMEZONE = ZoneInfo("Asia/Tashkent")

DB_FILE = "scheduler.db"

CHECK_INTERVAL = 5

MAX_TEXT_LENGTH = 4096


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")

    return connection


def init_db():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                run_at_utc TEXT NOT NULL,

                target_chat TEXT NOT NULL,

                content_type TEXT NOT NULL,

                text TEXT,

                file_id TEXT,

                caption TEXT,

                parse_mode TEXT,

                status TEXT NOT NULL DEFAULT 'pending',

                attempts INTEGER NOT NULL DEFAULT 0,

                last_error TEXT,

                created_at_utc TEXT NOT NULL,

                sent_at_utc TEXT
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_posts_due
            ON posts(status, run_at_utc)
            """
        )


def create_post(
    run_at_utc: str,
    target_chat: str,
    content_type: str,
    text: str | None = None,
    file_id: str | None = None,
    caption: str | None = None,
    parse_mode: str | None = None,
):
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO posts (
                run_at_utc,
                target_chat,
                content_type,
                text,
                file_id,
                caption,
                parse_mode,
                status,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                run_at_utc,
                target_chat,
                content_type,
                text,
                file_id,
                caption,
                parse_mode,
                now,
            ),
        )

        return cursor.lastrowid


def get_pending_posts():
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM posts
            WHERE status = 'pending'
              AND run_at_utc <= ?
            ORDER BY run_at_utc ASC, id ASC
            LIMIT 20
            """,
            (now,),
        ).fetchall()

    return rows


def get_upcoming_posts():
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM posts
            WHERE status = 'pending'
              AND run_at_utc > ?
            ORDER BY run_at_utc ASC
            LIMIT 50
            """,
            (now,),
        ).fetchall()

    return rows


def get_post(post_id: int):
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM posts
            WHERE id = ?
            """,
            (post_id,),
        ).fetchone()

    return row


def delete_post(post_id: int) -> bool:
    with get_db() as db:
        cursor = db.execute(
            """
            DELETE FROM posts
            WHERE id = ?
              AND status = 'pending'
            """,
            (post_id,),
        )

        return cursor.rowcount > 0


def mark_sent(post_id: int):
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as db:
        db.execute(
            """
            UPDATE posts
            SET status = 'sent',
                sent_at_utc = ?,
                last_error = NULL
            WHERE id = ?
            """,
            (now, post_id),
        )


def mark_error(post_id: int, error: str):
    with get_db() as db:
        db.execute(
            """
            UPDATE posts
            SET attempts = attempts + 1,
                last_error = ?
            WHERE id = ?
            """,
            (error[:1000], post_id),
        )


# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================

def is_admin(update: Update) -> bool:
    user = update.effective_user

    return (
        user is not None
        and user.id == ADMIN_ID
    )


async def admin_only(update: Update) -> bool:
    if not is_admin(update):
        return False

    return True


def parse_local_datetime(date_text: str, time_text: str):
    try:
        local_dt = datetime.strptime(
            f"{date_text} {time_text}",
            "%Y-%m-%d %H:%M",
        )

        local_dt = local_dt.replace(
            tzinfo=TIMEZONE
        )

        now = datetime.now(TIMEZONE)

        if local_dt <= now:
            return None

        return local_dt.astimezone(timezone.utc)

    except ValueError:
        return None


def format_local_time(utc_iso: str) -> str:
    dt = datetime.fromisoformat(utc_iso)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    local_dt = dt.astimezone(TIMEZONE)

    return local_dt.strftime(
        "%Y-%m-%d %H:%M"
    )


def normalize_chat_id(value: str) -> str:
    value = value.strip()

    if not value:
        raise ValueError

    return value


def get_help_text():
    return (
        "📅 Scheduler Bot\n\n"

        "Post rejalashtirish:\n"
        "/schedule SANA VAQT CHAT_ID MATN\n\n"

        "Masalan:\n"
        "/schedule 2026-09-25 18:30 "
        "-1001234567890 Salom, bu test post.\n\n"

        "Buyruqlar:\n"
        "/start - botni ishga tushirish\n"
        "/help - yordam\n"
        "/schedule - post rejalashtirish\n"
        "/list - rejalashtirilgan postlar\n"
        "/delete ID - postni o‘chirish\n"
        "/chatid - joriy chat ID\n"
        "/test CHAT_ID MATN - test yuborish\n"
        "/status - bot holati\n\n"

        "Vaqt: Toshkent vaqti (UTC+5)"
    )


# ============================================================
# COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    await update.message.reply_text(
        "✅ Scheduler bot ishlayapti.\n\n"
        "Yordam uchun /help buyrug‘ini yuboring."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    await update.message.reply_text(
        get_help_text()
    )


async def chatid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    chat = update.effective_chat

    await update.message.reply_text(
        f"Chat ID:\n`{chat.id}`",
        parse_mode="Markdown",
    )


async def schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Format:\n\n"
            "/schedule YYYY-MM-DD HH:MM CHAT_ID MATN\n\n"
            "Misol:\n"
            "/schedule 2026-09-25 18:30 "
            "-1001234567890 Test post"
        )
        return

    raw = update.message.text

    parts = raw.split(maxsplit=4)

    if len(parts) < 5:
        await update.message.reply_text(
            "❌ Format noto‘g‘ri.\n\n"
            "To‘g‘ri format:\n"
            "/schedule YYYY-MM-DD HH:MM CHAT_ID MATN"
        )
        return

    date_text = parts[1]
    time_text = parts[2]
    chat_text = parts[3]
    message_text = parts[4].strip()

    if len(message_text) > MAX_TEXT_LENGTH:
        await update.message.reply_text(
            f"❌ Matn juda uzun.\n"
            f"Telegram limiti: {MAX_TEXT_LENGTH} belgi."
        )
        return

    run_at = parse_local_datetime(
        date_text,
        time_text,
    )

    if run_at is None:
        await update.message.reply_text(
            "❌ Sana yoki vaqt noto‘g‘ri.\n\n"
            "Format:\n"
            "YYYY-MM-DD HH:MM\n\n"
            "Masalan:\n"
            "2026-09-25 18:30"
        )
        return

    try:
        target_chat = normalize_chat_id(chat_text)
    except ValueError:
        await update.message.reply_text(
            "❌ Chat ID noto‘g‘ri."
        )
        return

    post_id = create_post(
        run_at_utc=run_at.isoformat(),
        target_chat=target_chat,
        content_type="text",
        text=message_text,
    )

    await update.message.reply_text(
        "✅ Post rejalashtirildi.\n\n"
        f"ID: {post_id}\n"
        f"Chat: {target_chat}\n"
        f"Vaqt: {run_at.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Matn:\n{message_text}"
    )


async def list_posts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    posts = get_upcoming_posts()

    if not posts:
        await update.message.reply_text(
            "📭 Hozircha rejalashtirilgan post yo‘q."
        )
        return

    lines = [
        "📅 Rejalashtirilgan postlar:",
        "",
    ]

    for post in posts:
        text = post["text"] or post["caption"] or ""

        if len(text) > 120:
            text = text[:117] + "..."

        lines.append(
            f"ID: {post['id']}\n"
            f"🕐 {format_local_time(post['run_at_utc'])}\n"
            f"💬 {post['target_chat']}\n"
            f"📝 {text}\n"
            f"────────────"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


async def delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Format:\n/delete ID"
        )
        return

    try:
        post_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ ID raqam bo‘lishi kerak."
        )
        return

    post = get_post(post_id)

    if post is None:
        await update.message.reply_text(
            "❌ Bunday post topilmadi."
        )
        return

    if post["status"] != "pending":
        await update.message.reply_text(
            "❌ Bu postni o‘chirib bo‘lmaydi.\n"
            f"Holati: {post['status']}"
        )
        return

    if delete_post(post_id):
        await update.message.reply_text(
            f"✅ {post_id}-ID'li post o‘chirildi."
        )
    else:
        await update.message.reply_text(
            "❌ Postni o‘chirishning iloji bo‘lmadi."
        )


async def test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    raw = update.message.text
    parts = raw.split(maxsplit=2)

    if len(parts) < 3:
        await update.message.reply_text(
            "Format:\n\n"
            "/test CHAT_ID MATN\n\n"
            "Masalan:\n"
            "/test -1001234567890 Test xabar"
        )
        return

    target_chat = parts[1]
    message_text = parts[2]

    try:
        await context.bot.send_message(
            chat_id=target_chat,
            text=message_text,
        )

        await update.message.reply_text(
            "✅ Test xabar yuborildi."
        )

    except TelegramError as error:
        await update.message.reply_text(
            "❌ Xabar yuborilmadi.\n\n"
            f"{error}"
        )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    with get_db() as db:
        pending = db.execute(
            """
            SELECT COUNT(*)
            FROM posts
            WHERE status = 'pending'
            """
        ).fetchone()[0]

        sent = db.execute(
            """
            SELECT COUNT(*)
            FROM posts
            WHERE status = 'sent'
            """
        ).fetchone()[0]

        failed = db.execute(
            """
            SELECT COUNT(*)
            FROM posts
            WHERE last_error IS NOT NULL
            """
        ).fetchone()[0]

    await update.message.reply_text(
        "📊 Bot holati\n\n"
        f"⏳ Kutilayotgan: {pending}\n"
        f"✅ Yuborilgan: {sent}\n"
        f"⚠️ Xatolik qayd etilgan: {failed}\n"
        f"🕐 Vaqt zonasi: Asia/Tashkent"
    )


# ============================================================
# POST YUBORISH
# ============================================================

async def send_post(
    application: Application,
    post,
):
    content_type = post["content_type"]
    target_chat = post["target_chat"]

    if content_type == "text":

        await application.bot.send_message(
            chat_id=target_chat,
            text=post["text"],
        )

        return

    if content_type == "photo":

        await application.bot.send_photo(
            chat_id=target_chat,
            photo=post["file_id"],
            caption=post["caption"],
        )

        return

    if content_type == "video":

        await application.bot.send_video(
            chat_id=target_chat,
            video=post["file_id"],
            caption=post["caption"],
        )

        return

    if content_type == "document":

        await application.bot.send_document(
            chat_id=target_chat,
            document=post["file_id"],
            caption=post["caption"],
        )

        return

    if content_type == "audio":

        await application.bot.send_audio(
            chat_id=target_chat,
            audio=post["file_id"],
            caption=post["caption"],
        )

        return

    if content_type == "animation":

        await application.bot.send_animation(
            chat_id=target_chat,
            animation=post["file_id"],
            caption=post["caption"],
        )

        return

    raise ValueError(
        f"Noma'lum content_type: {content_type}"
    )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler_loop(
    application: Application,
):
    logger.info(
        "Scheduler ishga tushdi."
    )

    while True:

        try:
            posts = get_pending_posts()

            for post in posts:

                post_id = post["id"]

                try:

                    logger.info(
                        "Post yuborilmoqda: %s",
                        post_id,
                    )

                    await send_post(
                        application,
                        post,
                    )

                    mark_sent(post_id)

                    logger.info(
                        "Post yuborildi: %s",
                        post_id,
                    )

                except RetryAfter as error:

                    logger.warning(
                        "Telegram rate limit: %s soniya",
                        error.retry_after,
                    )

                    await asyncio.sleep(
                        float(error.retry_after)
                    )

                    break

                except (
                    Forbidden,
                    BadRequest,
                ) as error:

                    error_text = str(error)

                    logger.error(
                        "Post yuborilmadi %s: %s",
                        post_id,
                        error_text,
                    )

                    mark_error(
                        post_id,
                        error_text,
                    )

                    try:
                        await application.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                "⚠️ Post yuborilmadi.\n\n"
                                f"ID: {post_id}\n"
                                f"Chat: {post['target_chat']}\n"
                                f"Xato: {error_text}"
                            ),
                        )
                    except TelegramError:
                        pass

                except (
                    TimedOut,
                    NetworkError,
                ) as error:

                    error_text = str(error)

                    logger.warning(
                        "Tarmoq xatosi %s: %s",
                        post_id,
                        error_text,
                    )

                    mark_error(
                        post_id,
                        error_text,
                    )

                    break

                except Exception as error:

                    error_text = str(error)

                    logger.exception(
                        "Kutilmagan xato: %s",
                        post_id,
                    )

                    mark_error(
                        post_id,
                        error_text,
                    )

        except Exception:
            logger.exception(
                "Scheduler loop xatosi."
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def post_init(
    application: Application,
):
    init_db()

    await application.bot.set_my_commands(
        [
            ("start", "Botni ishga tushirish"),
            ("help", "Yordam"),
            ("schedule", "Post rejalashtirish"),
            ("list", "Rejalashtirilgan postlar"),
            ("delete", "Postni o‘chirish"),
            ("chatid", "Chat ID"),
            ("test", "Test xabar"),
            ("status", "Bot holati"),
        ]
    )

    application.bot_data["scheduler_task"] = (
        asyncio.create_task(
            scheduler_loop(application)
        )
    )

    logger.info(
        "Bot ishga tushdi."
    )


async def post_shutdown(
    application: Application,
):
    task = application.bot_data.get(
        "scheduler_task"
    )

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info(
        "Bot to‘xtadi."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Update xatosi: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN topilmadi. "
            "GitHub Secret nomi aynan BOT_TOKEN bo‘lishi kerak."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(
            Defaults(tzinfo=TIMEZONE)
        )
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "schedule",
            schedule,
        )
    )

    application.add_handler(
        CommandHandler(
            "list",
            list_posts,
        )
    )

    application.add_handler(
        CommandHandler(
            "delete",
            delete,
        )
    )

    application.add_handler(
        CommandHandler(
            "chatid",
            chatid,
        )
    )

    application.add_handler(
        CommandHandler(
            "test",
            test,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Polling boshlanmoqda..."
    )

    application.run_polling(
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
