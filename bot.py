import os
import psycopg2
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

TZ = timezone(timedelta(hours=-3))


# ---------------- DB ----------------

def db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT,
        chat_title TEXT,
        thread_id BIGINT,
        user_name TEXT,
        text TEXT,
        created TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()


def save(update: Update):
    if not update.message or not update.message.text:
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO messages(chat_id, chat_title, thread_id, user_name, text, created)
    VALUES (%s,%s,%s,%s,%s,%s)
    """, (
        update.effective_chat.id,
        update.effective_chat.title,
        update.message.message_thread_id,
        update.effective_user.full_name,
        update.message.text,
        datetime.now(TZ)
    ))

    conn.commit()
    conn.close()


# ---------------- HELPERS ----------------

def parse_date(txt):
    if txt == "hoje":
        return datetime.now(TZ).date()

    if txt == "ontem":
        return (datetime.now(TZ) - timedelta(days=1)).date()

    try:
        return datetime.strptime(txt, "%d/%m/%Y").date()
    except:
        return None


# ---------------- COMMANDS ----------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TARGET_CHAT_ID:
        return

    await update.message.reply_text(
        "🤖 Online.\n\nUse:\n"
        "/relatorio hoje\n"
        "/relatorio ontem\n"
        "/relatorio DD/MM/AAAA"
    )


async def relatorio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TARGET_CHAT_ID:
        return

    if not ctx.args:
        await update.message.reply_text("Use: /relatorio hoje | ontem | DD/MM/AAAA")
        return

    d = parse_date(ctx.args[0])
    if not d:
        await update.message.reply_text("Data inválida.")
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT chat_title, thread_id, user_name, text
    FROM messages
    WHERE DATE(created)=%s
    ORDER BY chat_title
    """, (d,))

    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Nada encontrado.")
        return

    out = f"🧾 RELATÓRIO BRUTO — {d.strftime('%d/%m/%Y')}\n\n"

    for g, t, u, m in rows:
        out += f"[{g}]\n{u}: {m}\n\n"

    await update.message.reply_text(out[:4000])


# ---------------- MAIN ----------------

async def capture(update: Update, ctx):
    if update.effective_chat.type in ["group", "supergroup"]:
        save(update)


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("relatorio", relatorio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
