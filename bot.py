import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone, date

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from openai import OpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Defina BOT_TOKEN e OPENAI_API_KEY nas variáveis do Railway")

TZ = timezone(timedelta(hours=-3))
DB_PATH = "data.db"
client = OpenAI(api_key=OPENAI_API_KEY)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            user_name TEXT,
            text TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_message(chat_id: int, user_name: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages(chat_id, user_name, text, created_at) VALUES (?,?,?,?)",
        (chat_id, user_name, text, datetime.now(TZ).isoformat())
    )
    conn.commit()
    conn.close()

def parse_date_from_text(txt: str) -> date | None:
    m = re.search(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", txt)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    return date(yyyy, mm, dd)

def fetch_messages_for_day(d: date, limit: int = 2500):
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ).isoformat()
    end = (datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ) + timedelta(days=1)).isoformat()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT user_name, text FROM messages WHERE created_at >= ? AND created_at < ? ORDER BY created_at ASC LIMIT ?",
        (start, end, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def build_prompt(d: date, rows):
    lines = []
    for user, text in rows:
        if not text:
            continue
        t = text.strip()
        if len(t) > 400:
            t = t[:400] + "…"
        lines.append(f"{user}: {t}")

    body = "\n".join(lines)

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n\n"
        "Crie um resumo operacional com:\n"
        "1) Principais assuntos\n"
        "2) Reclamações / problemas (quem comentou + resumo)\n"
        "3) Observações importantes\n"
        "4) O que melhorar / próximas ações (itens práticos)\n"
        "5) Quem mais participou (top 5)\n\n"
        "Mensagens:\n"
        f"{body}"
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Online. Use /resumo")

async def cmd_resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    d = parse_date_from_text(txt) or datetime.now(TZ).date()

    rows = fetch_messages_for_day(d)
    if not rows:
        await update.message.reply_text("Não encontrei mensagens.")
        return

    await update.message.reply_text("Gerando resumo...")

    prompt = build_prompt(d, rows)
    resp = client.responses.create(model="gpt-4.1-mini", input=prompt)
    out = (resp.output_text or "").strip()
    await update.message.reply_text(out)

async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        save_message(update.message.chat_id, update.message.from_user.full_name, update.message.text)

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resumo", cmd_resumo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))
    app.run_polling()

if __name__ == "__main__":
    main()
