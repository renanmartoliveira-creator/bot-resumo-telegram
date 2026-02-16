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
DATABASE_URL = os.getenv("DATABASE_URL")

TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))  # chat_id do "Resumo RGL"
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

TZ = timezone(timedelta(hours=-3))


# ---------------- DB ----------------

def db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = db()
    cur = conn.cursor()

    # 1) cria tabela (se não existir)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY
    )
    """)
    conn.commit()

    # 2) migração: adiciona colunas que podem estar faltando
    # (Isso resolve exatamente seu erro do chat_title)
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS chat_id BIGINT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS chat_title TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS thread_id BIGINT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS user_id BIGINT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS user_name TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS text TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS created TIMESTAMP")
    conn.commit()

    conn.close()


def save_message(update: Update):
    if not update.message or not update.message.text:
        return

    # grava só mensagens de group/supergroup (inclusive tópicos)
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return

    # não grava o próprio Resumo RGL (pra não poluir)
    if update.effective_chat.id == TARGET_CHAT_ID:
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO messages(chat_id, chat_title, thread_id, user_id, user_name, text, created)
    VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (
        update.effective_chat.id,
        update.effective_chat.title,
        update.message.message_thread_id,
        update.effective_user.id if update.effective_user else None,
        update.effective_user.full_name if update.effective_user else "SemNome",
        update.message.text,
        datetime.now(TZ),
    ))

    conn.commit()
    conn.close()


# ---------------- HELPERS ----------------

def parse_date_token(token: str):
    token = (token or "").strip().lower()

    if token == "hoje":
        return datetime.now(TZ).date()

    if token == "ontem":
        return (datetime.now(TZ) - timedelta(days=1)).date()

    try:
        return datetime.strptime(token, "%d/%m/%Y").date()
    except:
        return None


def extract_arg_from_text(msg_text: str):
    """
    Aceita:
      /relatorio hoje
      /relatorio@meubot hoje
      /relatorio 15/02/2026
    """
    if not msg_text:
        return None
    parts = msg_text.strip().split()
    if len(parts) < 2:
        return None
    return parts[1]


def is_target_chat(update: Update):
    return update.effective_chat and update.effective_chat.id == TARGET_CHAT_ID


async def safe_reply(update: Update, text: str):
    await update.message.reply_text(text[:4000])


# ---------------- COMMANDS ----------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_target_chat(update):
        return

    await safe_reply(
        update,
        "🤖 Online.\n\n"
        "Use:\n"
        "/relatorio hoje\n"
        "/relatorio ontem\n"
        "/relatorio DD/MM/AAAA\n\n"
        "Dica (se tiver vários bots):\n"
        "/relatorio@resumoequipe_bot hoje\n\n"
        "/status para contagens.\n"
        "/ping para teste rápido."
    )


async def ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_target_chat(update):
        return
    await safe_reply(update, "✅ Pong! Estou online.")


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_target_chat(update):
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(DISTINCT chat_id) FROM messages WHERE chat_id IS NOT NULL")
    groups = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(DISTINCT (chat_id, thread_id)) FROM messages WHERE chat_id IS NOT NULL")
    topics = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM messages")
    msgs = cur.fetchone()[0] or 0

    conn.close()

    await safe_reply(update, f"📊 Status\n\nGrupos: {groups}\nTópicos: {topics}\nMensagens: {msgs}")


async def relatorio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_target_chat(update):
        return

    token = ctx.args[0] if ctx.args else extract_arg_from_text(update.message.text)
    if not token:
        await safe_reply(update, "Use: /relatorio hoje | ontem | DD/MM/AAAA")
        return

    d = parse_date_token(token)
    if not d:
        await safe_reply(update, "Data inválida. Use: hoje | ontem | DD/MM/AAAA")
        return

    await safe_reply(update, "🧾 Gerando relatório bruto...")

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT chat_title, thread_id, user_name, text
    FROM messages
    WHERE DATE(created) = %s
    ORDER BY chat_title, thread_id, id
    """, (d,))

    rows = cur.fetchall()
    conn.close()

    if not rows:
        await safe_reply(update, "Nada encontrado para essa data.")
        return

    out = f"🧾 RELATÓRIO BRUTO — {d.strftime('%d/%m/%Y')}\n\n"
    last_group = None
    last_thread = None

    for chat_title, thread_id, user_name, text in rows:
        if chat_title != last_group:
            out += f"\n🏷️ Grupo: {chat_title}\n"
            last_group = chat_title
            last_thread = None

        if thread_id != last_thread:
            out += f"🧵 Tópico(thread_id): {thread_id}\n"
            last_thread = thread_id

        out += f"- {user_name}: {text}\n"

        if len(out) > 3500:
            await safe_reply(update, out)
            out = ""

    if out.strip():
        await safe_reply(update, out)


# ---------------- CAPTURE ----------------

async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    save_message(update)


# ---------------- MAIN ----------------

def main():
    if not BOT_TOKEN or not DATABASE_URL or not TARGET_CHAT_ID:
        raise RuntimeError("Defina BOT_TOKEN, DATABASE_URL e TARGET_CHAT_ID nas variáveis do Railway")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("relatorio", relatorio))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
