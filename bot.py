import os
import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ================= ENV =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))  # Resumo RGL
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Defina BOT_TOKEN e OPENAI_API_KEY no Railway")

client = OpenAI(api_key=OPENAI_API_KEY)

TZ = timezone(timedelta(hours=-3))
DB = "data.db"

# ================= DB =================

def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            chat_id INTEGER,
            topic_id INTEGER,
            user TEXT,
            text TEXT,
            created TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_msg(chat_id, topic_id, user, text):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (chat_id, topic_id, user, text, datetime.now(TZ).isoformat())
    )
    conn.commit()
    conn.close()

# ================= UTIL =================

def is_admin(update: Update):
    return update.effective_user.id == ADMIN_USER_ID

def stats():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    groups = c.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0]
    topics = c.execute("SELECT COUNT(DISTINCT topic_id) FROM messages").fetchone()[0]
    msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    return groups, topics, msgs

# ================= CAPTURA GLOBAL =================

async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    topic = update.message.message_thread_id
    user = update.message.from_user.full_name
    text = update.message.text

    save_msg(chat_id, topic, user, text)

# ================= COMANDOS (SÓ NO RGL) =================

def only_rgl(func):
    async def wrapper(update: Update, ctx):
        if update.effective_chat.id != TARGET_CHAT_ID:
            return
        return await func(update, ctx)
    return wrapper

@only_rgl
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g, t, m = stats()
    await update.message.reply_text(
        f"🤖 Online\n\nStatus:\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}"
    )

@only_rgl
async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g, t, m = stats()
    await update.message.reply_text(
        f"📊 Status\n\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}"
    )

@only_rgl
async def resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB)
    since = datetime.now(TZ).date().isoformat()

    rows = conn.execute("""
        SELECT user,text FROM messages
        WHERE created >= ?
    """, (since,)).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text("Nenhuma mensagem hoje.")
        return

    texto = "\n".join([f"{u}: {t}" for u,t in rows])

    prompt = f"Faça um resumo profissional dessas mensagens:\n\n{texto}"

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )

    out = resp.output_text.strip()

    await update.message.reply_text(out)

@only_rgl
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"user_id: {update.effective_user.id}\nchat_id: {update.effective_chat.id}\nthread_id: {update.message.message_thread_id}"
    )

# ================= MAIN =================

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # captura GLOBAL (todos grupos)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    # comandos APENAS no Resumo RGL
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("resumo", resumo))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("id", cmd_id))

    app.run_polling()

if __name__ == "__main__":
    main()
