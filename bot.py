import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone, date
from collections import Counter

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

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

# Grupo "Resumo RGL"
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()

# Apenas este user_id pode usar o painel
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN nas variáveis do Railway")
if not OPENAI_API_KEY:
    raise RuntimeError("Defina OPENAI_API_KEY nas variáveis do Railway")
if not TARGET_CHAT_ID:
    raise RuntimeError("Defina TARGET_CHAT_ID (chat_id do Resumo RGL) nas variáveis do Railway")

TARGET_CHAT_ID_INT = int(TARGET_CHAT_ID)

client = OpenAI(api_key=OPENAI_API_KEY)

TZ = timezone(timedelta(hours=-3))
DB = "data.db"


# =========================
# DB
# =========================
def db_conn():
    return sqlite3.connect(DB)


def init_db():
    conn = db_conn()

    # mensagens capturadas
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            chat_id INTEGER,
            thread_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            text TEXT,
            created_at TEXT
        )
    """)

    # lista de grupos vistos (para menu)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_registry(
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT,
            last_seen TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat_date ON messages(chat_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat_thread_date ON messages(chat_id, thread_id, created_at)")

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(TZ).isoformat()


def upsert_chat(chat_id: int, title: str):
    conn = db_conn()
    conn.execute("""
        INSERT INTO chat_registry(chat_id, chat_title, last_seen)
        VALUES(?,?,?)
        ON CONFLICT(chat_id) DO UPDATE
        SET chat_title=excluded.chat_title,
            last_seen=excluded.last_seen
    """, (chat_id, title, now_iso()))
    conn.commit()
    conn.close()


def save_message(chat_id: int, thread_id: int, user_id: int, user_name: str, text: str):
    conn = db_conn()
    conn.execute(
        "INSERT INTO messages(chat_id, thread_id, user_id, user_name, text, created_at) VALUES (?,?,?,?,?,?)",
        (chat_id, thread_id, user_id, user_name, text, now_iso())
    )
    conn.commit()
    conn.close()


def list_chats():
    conn = db_conn()
    rows = conn.execute("SELECT chat_id, chat_title FROM chat_registry ORDER BY chat_title").fetchall()
    conn.close()
    return rows


def db_counts():
    conn = db_conn()
    groups = conn.execute("SELECT COUNT(DISTINCT chat_id) FROM chat_registry").fetchone()[0]
    topics = conn.execute("SELECT COUNT(DISTINCT chat_id || ':' || thread_id) FROM messages").fetchone()[0]
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    return groups, topics, msgs


def day_range(d: date):
    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat()


def list_threads_for_day(chat_id: int, d: date):
    start, end = day_range(d)
    conn = db_conn()
    rows = conn.execute("""
        SELECT DISTINCT thread_id
        FROM messages
        WHERE chat_id=? AND created_at>=? AND created_at<?
        ORDER BY thread_id ASC
    """, (chat_id, start, end)).fetchall()
    conn.close()
    return [int(r[0]) for r in rows]


def fetch_messages_general(chat_id: int, d: date, limit: int = 9000):
    start, end = day_range(d)
    conn = db_conn()
    rows = conn.execute("""
        SELECT thread_id, user_name, text
        FROM messages
        WHERE chat_id=? AND created_at>=? AND created_at<?
        ORDER BY created_at ASC
        LIMIT ?
    """, (chat_id, start, end, limit)).fetchall()
    conn.close()
    return rows


def fetch_messages_thread(chat_id: int, thread_id: int, d: date, limit: int = 8000):
    start, end = day_range(d)
    conn = db_conn()
    rows = conn.execute("""
        SELECT user_name, text
        FROM messages
        WHERE chat_id=? AND thread_id=? AND created_at>=? AND created_at<?
        ORDER BY created_at ASC
        LIMIT ?
    """, (chat_id, thread_id, start, end, limit)).fetchall()
    conn.close()
    return rows


def parse_date_ddmmyyyy(txt: str) -> date | None:
    m = re.search(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", txt)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


# =========================
# SECURITY / SCOPE
# =========================
def is_admin(update: Update) -> bool:
    if not ADMIN_USER_ID:
        return True
    return str(update.effective_user.id) == str(ADMIN_USER_ID)


def is_in_resumo_rgl(update: Update) -> bool:
    try:
        return int(update.effective_chat.id) == TARGET_CHAT_ID_INT
    except Exception:
        return False


async def send_to_resumo(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    # sempre envia para o Resumo RGL, nunca para grupos de trabalho
    await ctx.bot.send_message(chat_id=TARGET_CHAT_ID_INT, text=text)


# =========================
# PROMPTS (OpenAI)
# =========================
def build_prompt_general(d: date, chat_id: int, rows):
    # rows: (thread_id, user_name, text)
    # montar um texto compacto
    lines = []
    for tid, user, text in rows:
        t = (text or "").strip()
        if not t:
            continue
        if len(t) > 350:
            t = t[:350] + "…"
        # tid 0 = sem tópico
        lines.append(f"[T{tid}] {user}: {t}")

    joined = "\n".join(lines)

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n"
        f"Grupo chat_id: {chat_id}\n\n"
        "Você vai resumir mensagens de trabalho.\n"
        "Quero:\n"
        "1) Principais assuntos (em bullets)\n"
        "2) Reclamações / problemas (quem falou + resumo curto)\n"
        "3) Observações importantes\n"
        "4) Próximas ações / o que melhorar (bem prático)\n"
        "5) Quem mais participou (top 10)\n\n"
        "Regras:\n"
        "- Não invente.\n"
        "- Se estiver incerto, diga 'incerto'.\n\n"
        "Mensagens:\n"
        f"{joined}"
    )


def build_prompt_thread(d: date, chat_id: int, thread_id: int, rows):
    lines = []
    for user, text in rows:
        t = (text or "").strip()
        if not t:
            continue
        if len(t) > 350:
            t = t[:350] + "…"
        lines.append(f"{user}: {t}")

    joined = "\n".join(lines)

    label = "Sem tópico" if thread_id == 0 else f"Tópico {thread_id}"

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n"
        f"Grupo chat_id: {chat_id}\n"
        f"{label}\n\n"
        "Resumo operacional:\n"
        "1) Assuntos principais\n"
        "2) Reclamações / problemas (quem + resumo)\n"
        "3) Observações\n"
        "4) Ações / melhorias\n"
        "5) Participantes (top 10)\n\n"
        "Mensagens:\n"
        f"{joined}"
    )


def run_openai(prompt: str) -> str:
    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )
    out = (resp.output_text or "").strip()
    return out or "Resumo vazio."


# =========================
# KEYBOARDS (Menu)
# =========================
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Escolher grupo", callback_data="menu:chats")],
        [InlineKeyboardButton("🔄 Atualizar lista de grupos", callback_data="menu:refresh")],
    ])


def kb_chats(chats):
    buttons = []
    for cid, title in chats[:80]:
        label = (title or str(cid))[:35]
        buttons.append([InlineKeyboardButton(label, callback_data=f"pickchat:{cid}")])
    buttons.append([InlineKeyboardButton("⬅️ Voltar", callback_data="back:main")])
    return InlineKeyboardMarkup(buttons)


def kb_group_actions(chat_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Resumo GERAL (todos tópicos)", callback_data=f"mode:general:{chat_id}")],
        [InlineKeyboardButton("📅 Resumo por TÓPICO (listar tópicos da data escolhida)", callback_data=f"mode:topics:{chat_id}")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="menu:chats")],
    ])


def kb_date_pick(mode: str, chat_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Hoje", callback_data=f"date:{mode}:{chat_id}:today"),
            InlineKeyboardButton("Ontem", callback_data=f"date:{mode}:{chat_id}:yest"),
        ],
        [InlineKeyboardButton("📅 Data específica", callback_data=f"date:{mode}:{chat_id}:pick")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data=f"pickchat:{chat_id}")],
    ])


def kb_topics(chat_id: int, tids: list[int], d: date):
    buttons = []
    for tid in tids[:100]:
        label = "Sem tópico" if tid == 0 else f"Tópico {tid}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"picktid:{chat_id}:{tid}:{d.isoformat()}")])
    buttons.append([InlineKeyboardButton("⬅️ Voltar", callback_data=f"mode:topics:{chat_id}")])
    return InlineKeyboardMarkup(buttons)


# =========================
# COMMANDS (Somente no Resumo RGL)
# =========================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_in_resumo_rgl(update):
        return
    if not is_admin(update):
        await update.message.reply_text("Sem permissão.")
        return

    g, t, m = db_counts()
    await update.message.reply_text(
        f"🤖 Online\n\nStatus:\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}",
        reply_markup=kb_main()
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_in_resumo_rgl(update):
        return
    if not is_admin(update):
        await update.message.reply_text("Sem permissão.")
        return

    g, t, m = db_counts()
    await update.message.reply_text(f"📊 Status\n\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}")


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_in_resumo_rgl(update):
        # /id pode ser útil em qualquer lugar, mas você pediu focado no RGL;
        # se quiser liberar global, eu libero depois.
        return
    await update.message.reply_text(
        f"user_id: {update.effective_user.id}\n"
        f"chat_id: {update.effective_chat.id}\n"
        f"thread_id: {update.effective_message.message_thread_id}"
    )


# =========================
# CALLBACKS (Botões)
# =========================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # Só aceitar cliques no Resumo RGL
    if q.message.chat_id != TARGET_CHAT_ID_INT:
        return
    if not is_admin(update):
        await q.edit_message_text("Sem permissão.")
        return

    data = q.data or ""

    # voltar
    if data == "back:main":
        g, t, m = db_counts()
        await q.edit_message_text(
            f"🤖 Online\n\nStatus:\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}",
            reply_markup=kb_main()
        )
        return

    # refresh
    if data == "menu:refresh":
        chats = list_chats()
        g, t, m = db_counts()
        if not chats:
            await q.edit_message_text(
                "🔄 Atualizei.\n\nAinda não vi nenhum grupo.\n"
                "Coloque o bot nos grupos e mande mensagens novas.\n\n"
                f"Status: {g} grupos, {t} tópicos, {m} mensagens.",
                reply_markup=kb_main()
            )
            return
        await q.edit_message_text(
            f"🔄 Atualizei.\nStatus: {g} grupos, {t} tópicos, {m} mensagens.\n\n"
            "Escolha um grupo:",
            reply_markup=kb_chats(chats)
        )
        return

    # listar chats
    if data == "menu:chats":
        chats = list_chats()
        if not chats:
            g, t, m = db_counts()
            await q.edit_message_text(
                "Ainda não vi nenhum grupo.\n"
                "Coloque o bot nos grupos e mande mensagens novas.\n\n"
                f"Status: {g} grupos, {t} tópicos, {m} mensagens.",
                reply_markup=kb_main()
            )
            return
        await q.edit_message_text("Escolha um grupo:", reply_markup=kb_chats(chats))
        return

    # escolheu chat
    if data.startswith("pickchat:"):
        chat_id = int(data.split(":")[1])
        await q.edit_message_text(
            f"✅ Grupo selecionado: {chat_id}\n\nEscolha o tipo de resumo:",
            reply_markup=kb_group_actions(chat_id)
        )
        return

    # modo geral
    if data.startswith("mode:general:"):
        chat_id = int(data.split(":")[2])
        await q.edit_message_text("🧠 Resumo GERAL — escolha a data:", reply_markup=kb_date_pick("gen", chat_id))
        return

    # modo tópicos
    if data.startswith("mode:topics:"):
        chat_id = int(data.split(":")[2])
        await q.edit_message_text(
            "📅 Resumo por TÓPICO — escolha a data:",
            reply_markup=kb_date_pick("topics", chat_id)
        )
        return

    # data escolhida
    m = re.match(r"^date:(gen|topics):(-?\d+):(today|yest|pick)$", data)
    if m:
        mode = m.group(1)
        chat_id = int(m.group(2))
        choice = m.group(3)

        if choice == "today":
            d = datetime.now(TZ).date()
        elif choice == "yest":
            d = datetime.now(TZ).date() - timedelta(days=1)
        else:
            # pedir para digitar data
            ctx.user_data["awaiting_date"] = {"mode": mode, "chat_id": chat_id}
            await q.edit_message_text("Digite a data no formato DD/MM/AAAA (ex: 12/02/2026).")
            return

        # executar
        if mode == "gen":
            await q.edit_message_text("Gerando resumo geral… vou enviar aqui no Resumo RGL.")
            rows = fetch_messages_general(chat_id, d)
            if not rows:
                await send_to_resumo(ctx, f"📌 Resumo GERAL — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n(sem mensagens registradas)")
            else:
                prompt = build_prompt_general(d, chat_id, rows)
                out = run_openai(prompt)
                await send_to_resumo(ctx, f"📌 Resumo GERAL — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n{out}")

            await q.edit_message_text("✅ Enviado.", reply_markup=kb_main())
            return

        if mode == "topics":
            tids = list_threads_for_day(chat_id, d)
            if not tids:
                await q.edit_message_text(
                    "Não achei mensagens nessa data.\n"
                    "Obs: o bot só pega mensagens novas (sem histórico antigo).",
                    reply_markup=kb_main()
                )
                return

            await q.edit_message_text(
                f"Escolha um tópico ({d.strftime('%d/%m/%Y')}):",
                reply_markup=kb_topics(chat_id, tids, d)
            )
            return

    # escolheu tópico
    if data.startswith("picktid:"):
        parts = data.split(":")
        chat_id = int(parts[1])
        tid = int(parts[2])
        d = date.fromisoformat(parts[3])

        await q.edit_message_text("Gerando resumo do tópico… vou enviar aqui no Resumo RGL.")
        rows = fetch_messages_thread(chat_id, tid, d)
        label = "Sem tópico" if tid == 0 else f"Tópico {tid}"

        if not rows:
            await send_to_resumo(ctx, f"📌 Resumo — {label} — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n(sem mensagens registradas)")
        else:
            prompt = build_prompt_thread(d, chat_id, tid, rows)
            out = run_openai(prompt)
            await send_to_resumo(ctx, f"📌 Resumo — {label} — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n{out}")

        await q.edit_message_text("✅ Enviado.", reply_markup=kb_main())
        return

    await q.edit_message_text("Ação não reconhecida.", reply_markup=kb_main())


# =========================
# INPUT DE DATA (somente no Resumo RGL)
# =========================
async def on_text_in_resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_in_resumo_rgl(update):
        return
    if not is_admin(update):
        return

    pending = ctx.user_data.get("awaiting_date")
    if not pending:
        return

    txt = (update.message.text or "").strip()
    d = parse_date_ddmmyyyy(txt)
    if not d:
        await update.message.reply_text("Formato inválido. Use DD/MM/AAAA (ex: 12/02/2026).")
        return

    ctx.user_data["awaiting_date"] = None
    mode = pending["mode"]
    chat_id = int(pending["chat_id"])

    if mode == "gen":
        await update.message.reply_text("Gerando resumo geral… vou enviar aqui no Resumo RGL.")
        rows = fetch_messages_general(chat_id, d)
        if not rows:
            await send_to_resumo(ctx, f"📌 Resumo GERAL — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n(sem mensagens registradas)")
        else:
            prompt = build_prompt_general(d, chat_id, rows)
            out = run_openai(prompt)
            await send_to_resumo(ctx, f"📌 Resumo GERAL — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n{out}")

        await update.message.reply_text("✅ Enviado.", reply_markup=kb_main())
        return

    if mode == "topics":
        tids = list_threads_for_day(chat_id, d)
        if not tids:
            await update.message.reply_text(
                "Não achei mensagens nessa data.\n"
                "Obs: o bot só pega mensagens novas (sem histórico antigo).",
                reply_markup=kb_main()
            )
            return

        await update.message.reply_text(
            f"Escolha um tópico ({d.strftime('%d/%m/%Y')}):",
            reply_markup=kb_topics(chat_id, tids, d)
        )
        return


# =========================
# CAPTURE (todos os grupos, exceto Resumo RGL)
# =========================
async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id

    # nunca capturar o próprio Resumo RGL
    if int(chat_id) == TARGET_CHAT_ID_INT:
        return

    title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
    upsert_chat(chat_id, title)

    thread_id = update.message.message_thread_id
    # thread_id None => armazenar como 0 (sem tópico)
    thread_id = int(thread_id) if thread_id is not None else 0

    user_id = update.effective_user.id if update.effective_user else 0
    user_name = update.effective_user.full_name if update.effective_user else "SemNome"
    text = update.message.text

    save_message(chat_id, thread_id, user_id, user_name, text)


# =========================
# MAIN
# =========================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # comandos (painel) só funcionam no Resumo RGL (checado dentro dos handlers)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("id", cmd_id))

    # botões
    app.add_handler(CallbackQueryHandler(on_callback))

    # entrada de data digitada (apenas no Resumo RGL)
    app.add_handler(MessageHandler(filters.Chat(TARGET_CHAT_ID_INT) & filters.TEXT & ~filters.COMMAND, on_text_in_resumo))

    # captura global (todos os grupos de trabalho)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
