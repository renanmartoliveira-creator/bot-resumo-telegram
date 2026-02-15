import os
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Variáveis BOT_TOKEN / OPENAI_API_KEY não definidas")

client = OpenAI(api_key=OPENAI_API_KEY)

TZ = timezone(timedelta(hours=-3))
DB = "data.db"


# ---------------- DB ----------------

def db():
    return sqlite3.connect(DB)


def init_db():
    c = db()
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        chat_id INTEGER,
        topic TEXT,
        user TEXT,
        text TEXT,
        created TEXT
    )
    """)
    c.commit()
    c.close()


def save_msg(chat, topic, user, text):
    c = db()
    c.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (chat, topic, user, text, datetime.now(TZ).isoformat()),
    )
    c.commit()
    c.close()


# ---------------- CAPTURA GLOBAL ----------------

async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.message.chat
    if chat.type not in ["group", "supergroup"]:
        return

    topic = chat.title
    user = update.message.from_user.full_name
    text = update.message.text or ""

    save_msg(chat.id, topic, user, text)


# ---------------- STATUS ----------------

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = db()
    g = c.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0]
    t = c.execute("SELECT COUNT(DISTINCT topic) FROM messages").fetchone()[0]
    m = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    c.close()

    await update.message.reply_text(
        f"📊 Status\n\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}"
    )


# ---------------- START ----------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TARGET_CHAT_ID:
        return

    c = db()
    rows = c.execute("SELECT DISTINCT topic FROM messages").fetchall()
    c.close()

    if not rows:
        await update.message.reply_text("Ainda não há mensagens registradas.")
        return

    buttons = [[InlineKeyboardButton(r[0], callback_data=r[0])] for r in rows]
    await update.message.reply_text(
        "Escolha um grupo:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------------- CALLBACK ----------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topic = query.data

    c = db()
    rows = c.execute(
        "SELECT user,text FROM messages WHERE topic=?",
        (topic,),
    ).fetchall()
    c.close()

    content = "\n".join([f"{u}: {t}" for u, t in rows])

    prompt = f"Faça um resumo claro das mensagens abaixo:\n\n{content}"

    await query.message.reply_text("Gerando resumo...")

    result = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
    )

    out = result.choices[0].message.content

    await ctx.bot.send_message(chat_id=TARGET_CHAT_ID, text=out)


# ---------------- MAIN ----------------

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
