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

# Grupo onde o resumo será enviado
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()
TARGET_THREAD_ID = os.getenv("TARGET_THREAD_ID", "").strip()

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Defina BOT_TOKEN e OPENAI_API_KEY nas variáveis do Railway")

if not TARGET_CHAT_ID:
    raise RuntimeError("Defina TARGET_CHAT_ID nas variáveis do Railway (grupo de resumo)")

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_day ON messages(chat_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thread ON messages(chat_id, thread_id, created_at)")
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
    # aceita dd/mm/aaaa ou dd-mm-aaaa
    m = re.search(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", txt)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    return date(yyyy, mm, dd)


def fetch_messages_general_for_day(chat_id: int, d: date, limit: int = 4000):
    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ).isoformat()
    end_dt = (datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ) + timedelta(days=1)).isoformat()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """
        SELECT thread_id, user_name, text
        FROM messages
        WHERE chat_id = ?
          AND created_at >= ? AND created_at < ?
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (chat_id, start_dt, end_dt, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_prompt_general(d: date, rows):
    # Agrupa por tópico (thread_id)
    by_thread: dict[int, list[str]] = {}
    for thread_id, user, text in rows:
        tid = int(thread_id) if thread_id is not None else 0
        by_thread.setdefault(tid, [])
        t = (text or "").strip()
        if not t:
            continue
        if len(t) > 500:
            t = t[:500] + "…"
        by_thread[tid].append(f"{user}: {t}")

    blocks = []
    # Ordena para ficar consistente
    for tid in sorted(by_thread.keys()):
        title = f"TÓPICO {tid}" if tid != 0 else "SEM TÓPICO (mensagens fora de tópicos)"
        content = "\n".join(by_thread[tid][:600])
        blocks.append(title + "\n" + content)

    body = "\n\n---\n\n".join(blocks)

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n"
        "Você vai criar um RESUMO GERAL do dia juntando TODOS os tópicos.\n\n"
        "Entregue em blocos:\n"
        "1) Principais assuntos (separe por tópicos quando fizer sentido)\n"
        "2) Reclamações / problemas (quem falou + resumo curto)\n"
        "3) Observações importantes\n"
        "4) O que melhorar / próximas ações (itens práticos)\n"
        "5) Quem mais participou (top 5)\n\n"
        "Regras:\n"
        "- Não invente nada.\n"
        "- Não copie textos longos, apenas resuma.\n"
        "- Se faltar contexto, diga 'incerto'.\n\n"
        "Mensagens do dia (organizadas por tópico):\n"
        f"{body}"
    )


async def send_to_target(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    kwargs = {}
    if TARGET_THREAD_ID:
        kwargs["message_thread_id"] = int(TARGET_THREAD_ID)
    await ctx.bot.send_message(chat_id=int(TARGET_CHAT_ID), text=text, **kwargs)


# ====== COMANDOS ======
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Online.\n\n"
        "Comandos:\n"
        "/id  → mostra chat_id e thread_id\n"
        "/resumo  → RESUMO GERAL de hoje (manda só no grupo de resumo)\n"
        "/resumo 12/02/2026 → RESUMO GERAL da data\n\n"
        "Obs: eu NÃO envio o resumo aqui; envio apenas no grupo de resumo."
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    await update.message.reply_text(f"chat_id: {chat_id}\nthread_id: {thread_id}")


async def cmd_resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    d = parse_date_from_text(txt) or datetime.now(TZ).date()

    source_chat_id = update.effective_chat.id

    # Evita loop: se alguém mandar /resumo no grupo de resumo, não faz nada
    if str(source_chat_id) == str(TARGET_CHAT_ID):
        await update.message.reply_text("⚠️ Envie /resumo no grupo que você quer resumir, não no grupo de resumo.")
        return

    rows = fetch_messages_general_for_day(source_chat_id, d)
    if not rows:
        await update.message.reply_text("Não encontrei mensagens desse dia (a partir do momento que o bot ficou online).")
        return

    # Pequeno ACK (não é o resumo)
    await update.message.reply_text("🧠 Gerando resumo geral e enviando no grupo de resumo...")

    prompt = build_prompt_general(d, rows)
    resp = client.responses.create(model="gpt-4.1-mini", input=prompt)
    out = (resp.output_text or "").strip() or "Resumo vazio (não retornou texto)."

    header = (
        f"📌 RESUMO GERAL — {d.strftime('%d/%m/%Y')}\n"
        f"Grupo origem (chat_id): {source_chat_id}\n"
    )

    await send_to_target(ctx, header + "\n" + out)

    # Confirmação curtinha (sem despejar resumo aqui)
    await update.message.reply_text("✅ Resumo enviado no grupo Resumo RGL.")


# ====== CAPTURAR MENSAGENS ======
async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id

    # Não salvar mensagens do grupo de resumo (evita poluir e loop)
    if str(chat_id) == str(TARGET_CHAT_ID):
        return

    thread_id = update.message.message_thread_id  # None se fora de tópico

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
