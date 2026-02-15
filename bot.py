import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone, date

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

# =======================
# CONFIG / ENV
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Grupo "Resumo RGL" (onde fica o painel e onde o resumo é enviado)
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()

# (Opcional) tópico específico dentro do grupo Resumo RGL
TARGET_THREAD_ID = os.getenv("TARGET_THREAD_ID", "").strip()

# (Recomendado) só esse user_id pode usar o painel
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

# Timezone Brasil -03
TZ = timezone(timedelta(hours=-3))

DB = "data.db"

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN nas variáveis do Railway.")
if not OPENAI_API_KEY:
    raise RuntimeError("Defina OPENAI_API_KEY nas variáveis do Railway.")
if not TARGET_CHAT_ID:
    raise RuntimeError("Defina TARGET_CHAT_ID (chat_id do Resumo RGL) nas variáveis do Railway.")

client = OpenAI(api_key=OPENAI_API_KEY)


# =======================
# DB
# =======================
def db_conn():
    return sqlite3.connect(DB)

def init_db():
    conn = db_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            thread_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            text TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_registry (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topic_alias (
            chat_id INTEGER,
            thread_id INTEGER,
            alias TEXT,
            PRIMARY KEY(chat_id, thread_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgs_chat_date ON messages(chat_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgs_chat_thread_date ON messages(chat_id, thread_id, created_at)")
    conn.commit()
    conn.close()

def now_iso():
    return datetime.now(TZ).isoformat()

def day_range(d: date):
    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat()

def upsert_chat(chat_id: int, title: str):
    conn = db_conn()
    conn.execute("""
        INSERT INTO chat_registry(chat_id, chat_title, last_seen)
        VALUES(?,?,?)
        ON CONFLICT(chat_id) DO UPDATE
        SET chat_title=excluded.chat_title, last_seen=excluded.last_seen
    """, (chat_id, title, now_iso()))
    conn.commit()
    conn.close()

def save_message(chat_id: int, thread_id: int | None, user_id: int | None, user_name: str, text: str):
    conn = db_conn()
    conn.execute(
        "INSERT INTO messages(chat_id, thread_id, user_id, user_name, text, created_at) VALUES (?,?,?,?,?,?)",
        (chat_id, thread_id, user_id, user_name, text, now_iso()),
    )
    conn.commit()
    conn.close()

def list_chats():
    conn = db_conn()
    cur = conn.execute("SELECT chat_id, chat_title FROM chat_registry ORDER BY chat_title")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_alias(chat_id: int, thread_id: int):
    conn = db_conn()
    cur = conn.execute("SELECT alias FROM topic_alias WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_alias(chat_id: int, thread_id: int, alias: str):
    conn = db_conn()
    conn.execute("""
        INSERT INTO topic_alias(chat_id, thread_id, alias)
        VALUES(?,?,?)
        ON CONFLICT(chat_id, thread_id) DO UPDATE SET alias=excluded.alias
    """, (chat_id, thread_id, alias))
    conn.commit()
    conn.close()

def list_threads_in_chat_for_day(chat_id: int, d: date):
    start, end = day_range(d)
    conn = db_conn()
    cur = conn.execute("""
        SELECT DISTINCT COALESCE(thread_id, 0) as tid
        FROM messages
        WHERE chat_id=? AND created_at>=? AND created_at<?
        ORDER BY tid
    """, (chat_id, start, end))
    tids = [r[0] for r in cur.fetchall()]
    conn.close()
    return tids

def fetch_general(chat_id: int, d: date, limit: int = 8000):
    start, end = day_range(d)
    conn = db_conn()
    cur = conn.execute("""
        SELECT COALESCE(thread_id, 0) as tid, user_name, text
        FROM messages
        WHERE chat_id=? AND created_at>=? AND created_at<?
        ORDER BY created_at ASC
        LIMIT ?
    """, (chat_id, start, end, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def fetch_thread(chat_id: int, tid: int, d: date, limit: int = 6000):
    start, end = day_range(d)
    conn = db_conn()
    if tid == 0:
        cur = conn.execute("""
            SELECT user_name, text
            FROM messages
            WHERE chat_id=? AND thread_id IS NULL AND created_at>=? AND created_at<?
            ORDER BY created_at ASC
            LIMIT ?
        """, (chat_id, start, end, limit))
    else:
        cur = conn.execute("""
            SELECT user_name, text
            FROM messages
            WHERE chat_id=? AND thread_id=? AND created_at>=? AND created_at<?
            ORDER BY created_at ASC
            LIMIT ?
        """, (chat_id, tid, start, end, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def parse_date_from_text(txt: str) -> date | None:
    m = re.search(r"(\d{2})[\/\-](\d{2})[\/\-](\d{4})", txt)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    return date(yyyy, mm, dd)

def db_counts():
    conn = db_conn()
    grupos = conn.execute("SELECT COUNT(DISTINCT chat_id) FROM chat_registry").fetchone()[0]
    topicos = conn.execute("SELECT COUNT(DISTINCT chat_id || ':' || COALESCE(thread_id, 0)) FROM messages").fetchone()[0]
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    return grupos, topicos, msgs


# =======================
# SECURITY + SENDING
# =======================
def is_admin_user(update: Update) -> bool:
    if not ADMIN_USER_ID:
        return True
    try:
        return str(update.effective_user.id) == str(ADMIN_USER_ID)
    except Exception:
        return False

async def send_to_resumo(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    kwargs = {}
    if TARGET_THREAD_ID:
        kwargs["message_thread_id"] = int(TARGET_THREAD_ID)
    await ctx.bot.send_message(chat_id=int(TARGET_CHAT_ID), text=text, **kwargs)


# =======================
# PROMPTS
# =======================
def prompt_general(d: date, chat_id: int, rows):
    by_tid: dict[int, list[str]] = {}
    for tid, user, text in rows:
        tid = int(tid)
        by_tid.setdefault(tid, [])
        t = (text or "").strip()
        if not t:
            continue
        if len(t) > 350:
            t = t[:350] + "…"
        by_tid[tid].append(f"{user}: {t}")

    blocks = []
    for tid in sorted(by_tid.keys()):
        if tid == 0:
            title = "SEM TÓPICO"
        else:
            alias = get_alias(chat_id, tid)
            title = alias if alias else f"TÓPICO {tid}"
        blocks.append(title + "\n" + "\n".join(by_tid[tid]))

    body = "\n\n---\n\n".join(blocks)

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n"
        "Faça um RESUMO GERAL juntando TODOS os tópicos.\n\n"
        "Quero em blocos:\n"
        "1) Principais assuntos\n"
        "2) Reclamações / problemas (quem + resumo)\n"
        "3) Observações importantes\n"
        "4) O que melhorar / próximas ações (itens práticos)\n"
        "5) Quem mais participou (top 10)\n\n"
        "Regras:\n"
        "- Não invente.\n"
        "- Não copie longos trechos; resuma.\n"
        "- Se incerto, diga 'incerto'.\n\n"
        "Mensagens do dia:\n"
        f"{body}"
    )

def prompt_topic(d: date, topic_label: str, rows):
    lines = []
    for user, text in rows:
        t = (text or "").strip()
        if not t:
            continue
        if len(t) > 350:
            t = t[:350] + "…"
        lines.append(f"{user}: {t}")

    body = "\n".join(lines)

    return (
        f"Data: {d.strftime('%d/%m/%Y')} (fuso -03:00)\n"
        f"Tópico: {topic_label}\n\n"
        "Faça um resumo operacional:\n"
        "1) Assuntos principais\n"
        "2) Reclamações / problemas\n"
        "3) Observações\n"
        "4) Melhorias / ações\n"
        "5) Participantes (top 10)\n\n"
        "Mensagens:\n"
        f"{body}"
    )


# =======================
# UI / KEYBOARDS
# =======================
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Escolher grupo", callback_data="menu:chats")],
        [InlineKeyboardButton("🔄 Atualizar lista de grupos", callback_data="menu:refresh")],
    ])

def kb_chats(chats):
    buttons = []
    for cid, title in chats[:60]:
        label = (title or str(cid))[:35]
        buttons.append([InlineKeyboardButton(label, callback_data=f"pickchat:{cid}")])
    buttons.append([InlineKeyboardButton("⬅️ Voltar", callback_data="back:main")])
    return InlineKeyboardMarkup(buttons)

def kb_mode(chat_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Resumo GERAL (todos tópicos)", callback_data=f"mode:general:{chat_id}")],
        [InlineKeyboardButton("📅 Resumo por TÓPICO (escolher data)", callback_data=f"mode:topics:{chat_id}")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="menu:chats")],
    ])

def kb_date_choice(mode: str, chat_id: int):
    # mode: "gen" ou "topics"
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
    for tid in tids[:80]:
        if tid == 0:
            label = "Sem tópico"
        else:
            alias = get_alias(chat_id, tid)
            label = alias if alias else f"Tópico {tid}"
        buttons.append([InlineKeyboardButton(label[:35], callback_data=f"picktid:{chat_id}:{tid}:{d.isoformat()}")])
    buttons.append([InlineKeyboardButton("⬅️ Voltar", callback_data=f"mode:topics:{chat_id}")])
    return InlineKeyboardMarkup(buttons)


# =======================
# COMMANDS (only in Resumo RGL)
# =======================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(TARGET_CHAT_ID):
        return
    if not is_admin_user(update):
        await update.message.reply_text("Sem permissão.")
        return
    await update.message.reply_text("Painel de Resumos ✅", reply_markup=kb_main())

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    user_id = update.effective_user.id
    await update.message.reply_text(f"user_id: {user_id}\nchat_id: {chat_id}\nthread_id: {thread_id}")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(TARGET_CHAT_ID):
        return
    if not is_admin_user(update):
        await update.message.reply_text("Sem permissão.")
        return

    grupos, topicos, msgs = db_counts()
    await update.message.reply_text(
        "📊 STATUS DO BOT\n\n"
        f"Grupos vistos: {grupos}\n"
        f"Tópicos vistos: {topicos}\n"
        f"Mensagens gravadas: {msgs}\n\n"
        "Obs: o bot só conta o que chegou DEPOIS que ele ficou online."
    )

async def cmd_alias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /apelido_topico <thread_id> <apelido>
    if update.effective_chat.id != int(TARGET_CHAT_ID):
        # permitir também usar em grupos normais se você quiser; aqui vamos bloquear pra manter simples
        await update.message.reply_text("Use este comando no Resumo RGL.")
        return
    if not is_admin_user(update):
        await update.message.reply_text("Sem permissão.")
        return

    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Use: /apelido_topico <thread_id> <apelido>")
        return
    tid = int(parts[1])
    alias = parts[2].strip()
    # apelido só faz sentido por grupo; como você escolhe o grupo no painel,
    # este comando fica opcional e não é essencial. Mantendo simples:
    await update.message.reply_text(
        "✅ Apelido recebido.\n\n"
        "⚠️ Neste modelo, apelidos por tópico são por GRUPO.\n"
        "Se quiser, eu adiciono um botão no painel pra definir apelido no grupo escolhido."
    )


# =======================
# GENERATORS
# =======================
async def generate_and_send_general(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, d: date):
    rows = fetch_general(chat_id, d)
    if not rows:
        await send_to_resumo(ctx, f"📌 Resumo GERAL — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n(sem mensagens registradas)")
        return
    p = prompt_general(d, chat_id, rows)
    resp = client.responses.create(model="gpt-4.1-mini", input=p)
    out = (resp.output_text or "").strip() or "Resumo vazio."
    await send_to_resumo(ctx, f"📌 Resumo GERAL — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n{out}")

async def generate_and_send_topic(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, tid: int, d: date):
    rows = fetch_thread(chat_id, tid, d)
    if tid == 0:
        label = "Sem tópico"
    else:
        alias = get_alias(chat_id, tid)
        label = alias if alias else f"Tópico {tid}"

    if not rows:
        await send_to_resumo(ctx, f"📌 Resumo — {label} — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n(sem mensagens registradas)")
        return

    p = prompt_topic(d, label, rows)
    resp = client.responses.create(model="gpt-4.1-mini", input=p)
    out = (resp.output_text or "").strip() or "Resumo vazio."
    await send_to_resumo(ctx, f"📌 Resumo — {label} — {d.strftime('%d/%m/%Y')}\nGrupo: {chat_id}\n\n{out}")


# =======================
# CALLBACKS (buttons)
# =======================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.message.chat_id != int(TARGET_CHAT_ID):
        return
    if not is_admin_user(update):
        await q.edit_message_text("Sem permissão.")
        return

    data = q.data or ""

    if data == "back:main":
        await q.edit_message_text("Painel de Resumos ✅", reply_markup=kb_main())
        return

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
            "Escolha o grupo para resumir:",
            reply_markup=kb_chats(chats),
        )
        return

    if data == "menu:chats":
        chats = list_chats()
        if not chats:
            g, t, m = db_counts()
            await q.edit_message_text(
                "Ainda não vi nenhum grupo.\n"
                "Coloque o bot nos grupos e mande mensagens novas.\n\n"
                f"Status: {g} grupos, {t} tópicos, {m} mensagens.",
                reply_markup=kb_main(),
            )
            return
        await q.edit_message_text("Escolha o grupo para resumir:", reply_markup=kb_chats(chats))
        return

    if data.startswith("pickchat:"):
        chat_id = int(data.split(":")[1])
        await q.edit_message_text(f"Grupo selecionado: {chat_id}\nEscolha o tipo:", reply_markup=kb_mode(chat_id))
        return

    if data.startswith("mode:general:"):
        chat_id = int(data.split(":")[2])
        await q.edit_message_text("Resumo GERAL — escolha a data:", reply_markup=kb_date_choice("gen", chat_id))
        return

    if data.startswith("mode:topics:"):
        chat_id = int(data.split(":")[2])
        await q.edit_message_text("Resumo por TÓPICO — escolha a data:", reply_markup=kb_date_choice("topics", chat_id))
        return

    # Date handling
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
            ctx.user_data["awaiting_date"] = {"mode": mode, "chat_id": chat_id}
            await q.edit_message_text("Digite a data no formato DD/MM/AAAA (ex: 12/02/2026).")
            return

        if mode == "gen":
            await q.edit_message_text("Gerando resumo geral… (vou enviar aqui no Resumo RGL)")
            await generate_and_send_general(ctx, chat_id, d)
            await q.edit_message_text("✅ Enviado.", reply_markup=kb_main())
            return

        if mode == "topics":
            tids = list_threads_in_chat_for_day(chat_id, d)
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

    # Pick a topic for that date
    if data.startswith("picktid:"):
        # picktid:<chat_id>:<tid>:<iso-date>
        parts = data.split(":")
        chat_id = int(parts[1])
        tid = int(parts[2])
        d = date.fromisoformat(parts[3])

        await q.edit_message_text("Gerando resumo do tópico… (vou enviar aqui no Resumo RGL)")
        await generate_and_send_topic(ctx, chat_id, tid, d)
        await q.edit_message_text("✅ Enviado.", reply_markup=kb_main())
        return

    await q.edit_message_text("Ação não reconhecida.", reply_markup=kb_main())


# =======================
# DATE INPUT IN RESUMO RGL (when user types dd/mm/yyyy)
# =======================
async def on_text_in_resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(TARGET_CHAT_ID):
        return
    if not is_admin_user(update):
        return

    pending = ctx.user_data.get("awaiting_date")
    if not pending:
        return

    txt = (update.message.text or "").strip()
    d = parse_date_from_text(txt)
    if not d:
        await update.message.reply_text("Formato inválido. Use DD/MM/AAAA.")
        return

    ctx.user_data["awaiting_date"] = None

    mode = pending["mode"]
    chat_id = int(pending["chat_id"])

    if mode == "gen":
        await update.message.reply_text("Gerando resumo geral…")
        await generate_and_send_general(ctx, chat_id, d)
        await update.message.reply_text("✅ Enviado.", reply_markup=kb_main())
        return

    if mode == "topics":
        tids = list_threads_in_chat_for_day(chat_id, d)
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


# =======================
# CAPTURE MESSAGES (all groups except Resumo RGL)
# =======================
async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id

    # NUNCA salvar mensagens do Resumo RGL (painel)
    if str(chat_id) == str(TARGET_CHAT_ID):
        return

    title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
    upsert_chat(chat_id, title)

    thread_id = update.message.message_thread_id
    user_id = update.effective_user.id if update.effective_user else None
    user_name = update.effective_user.full_name if update.effective_user else "SemNome"
    save_message(chat_id, thread_id, user_id, user_name, update.message.text)


# =======================
# MAIN
# =======================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # comandos no Resumo RGL
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("apelido_topico", cmd_alias))

    # botões
    app.add_handler(CallbackQueryHandler(on_callback))

    # texto digitado no Resumo RGL para data DD/MM/AAAA
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_in_resumo))

    # captura geral em grupos (menos Resumo RGL)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
