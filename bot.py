import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone, date

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from openai import OpenAI

# ====== VARIÁVEIS (Railway) ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Grupo onde o resumo será enviado automaticamente
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()
TARGET_THREAD_ID = os.getenv("TARGET_THREAD_ID", "").strip()

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Defina BOT_TOKEN e OPENAI_API_KEY nas variáveis do Railway")

# Fuso do Brasil (-03:00)
TZ = timezone(timedelta(hours=-3))

DB_PATH = "data.db"
client = OpenAI(api_key=OPENAI_API_KEY)


# ====== BANCO ======
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            thread_id INTEGER,
            user_name TEXT,
            text TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_message(chat_id: int, thread_id: int | None, user_name: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages(chat_id, thread_id, user_name, text, created_at) VALUES (?,?,?,?,?)",
        (chat_id, thread_id, user_name, text, datetime.now(TZ).isoformat())
    )
    conn.commit()
    conn.close()


def parse_date_from_text(txt: str) -> date | None:
    m = re.search(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", txt)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    return date(yyyy, mm, dd)


def fetch_messages_for_day(chat_id: int, thread_id: int | None, d: date, limit: int = 2500):
    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ).isoformat()
    end_dt = (datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ) + timedelta(days=1)).isoformat()

    conn = sqlite3.connect(DB_PATH)

    if thread_id is None:
        cur = conn.execute(
            """
            SELECT user_name, text
            FROM messages
            WHERE chat_id = ?
              AND (thread_id IS NULL)
              AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (chat_id, start_dt, end_dt, limit)
        )
    else:
        cur = conn.execute(
            """
            SELECT user_name, text
            FROM messages
            WHERE chat_id = ?
              AND thread_id = ?
              AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (chat_id, thread_id, start_dt, end_dt, limit)
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
        if len(t) > 500:
            t = t[:500] + "…"
        lines.append(f"{user}: {t}")

    body = "\n".join(lines)

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n\n"
        "Faça um resumo operacional do que aconteceu no chat, com:\n"
        "1) Principais assuntos\n"
        "2) Reclamações / problemas (quem falou + resumo)\n"
        "3) Observações importantes\n"
        "4) O que melhorar / próximas ações (itens práticos)\n"
        "5) Quem mais participou (top 5)\n\n"
        "Regras:\n"
        "- Não invente nada.\n"
        "- Não copie mensagens longas, apenas resuma.\n"
        "- Se faltar contexto, diga 'incerto'.\n\n"
        "Mensagens:\n"
        f"{body}"
    )


# ====== COMANDOS ======
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Online.\n\n"
        "Comandos:\n"
        "/id  → mostra chat_id e thread_id\n"
        "/resumo  → resumo de hoje (do tópico atual)\n"
        "/resumo 12/02/2026 → resumo de uma data\n\n"
        "Obs: quando você pedir /resumo, eu também envio para o grupo de Resumo (se TARGET_CHAT_ID estiver configurado)."
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    await update.message.reply_text(f"chat_id: {chat_id}\nthread_id: {thread_id}")


async def send_to_target(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    if not TARGET_CHAT_ID:
        return
    kwargs = {}
    if TARGET_THREAD_ID:
        kwargs["message_thread_id"] = int(TARGET_THREAD_ID)
    await ctx.bot.send_message(chat_id=int(TARGET_CHAT_ID), text=text, **kwargs)


async def cmd_resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    d = parse_date_from_text(txt) or datetime.now(TZ).date()

    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    rows = fetch_messages_for_day(chat_id, thread_id, d)
    if not rows:
        await update.message.reply_text("Não encontrei mensagens nesse dia (neste tópico).")
        return

    await update.message.reply_text("🧠 Gerando resumo...")

    prompt = build_prompt(d, rows)
    resp = client.responses.create(model="gpt-4.1-mini", input=prompt)
    out = (resp.output_text or "").strip() or "Resumo vazio (não retornou texto)."

    # Responde onde você pediu
    await update.message.reply_text(out)

    # E envia para o Grupo do Resumo
    header = f"📌 Resumo do dia {d.strftime('%d/%m/%Y')} (tópico atual)\n"
    await send_to_target(ctx, header + "\n" + out)


# ====== CAPTURAR MENSAGENS ======
async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    user_name = "SemNome"
    if update.message.from_user:
        user_name = update.message.from_user.full_name or update.message.from_user.username or "SemNome"

    save_message(chat_id, thread_id, user_name, update.message.text)


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("resumo", cmd_resumo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
