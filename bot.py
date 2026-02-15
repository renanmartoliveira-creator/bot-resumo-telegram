import os
import re
import json
import logging
from datetime import datetime, timedelta, date, timezone

import psycopg2
import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("resumo-bot")

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or "0")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0") or "0")

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN nas variáveis do Railway.")
if not OPENAI_API_KEY:
    raise RuntimeError("Defina OPENAI_API_KEY nas variáveis do Railway.")
if not DATABASE_URL:
    raise RuntimeError("Defina DATABASE_URL nas variáveis do Railway (Postgres).")
if not TARGET_CHAT_ID:
    raise RuntimeError("Defina TARGET_CHAT_ID (chat_id do grupo Resumo RGL).")

# Brasil (GMT-3)
TZ = timezone(timedelta(hours=-3))

# =========================
# DB helpers
# =========================
def db_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    # mensagens
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            thread_id BIGINT NULL,
            message_id BIGINT NOT NULL,
            user_id BIGINT NULL,
            user_name TEXT NULL,
            text TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_thread_time ON messages(chat_id, thread_id, created_at);")

    # chats vistos (pra listar grupos no menu)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id BIGINT PRIMARY KEY,
            title TEXT NULL,
            chat_type TEXT NULL,
            last_seen TIMESTAMPTZ NOT NULL
        );
        """
    )

    conn.close()

def upsert_chat(chat_id: int, title: str | None, chat_type: str | None):
    conn = db_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chats(chat_id, title, chat_type, last_seen)
        VALUES(%s, %s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
            title = EXCLUDED.title,
            chat_type = EXCLUDED.chat_type,
            last_seen = EXCLUDED.last_seen
        """,
        (chat_id, title, chat_type, datetime.now(tz=TZ)),
    )
    conn.close()

def save_message(chat_id: int, thread_id: int | None, message_id: int, user_id: int | None, user_name: str | None, text: str | None, created_at: datetime):
    conn = db_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO messages(chat_id, thread_id, message_id, user_id, user_name, text, created_at)
        VALUES(%s, %s, %s, %s, %s, %s, %s)
        """,
        (chat_id, thread_id, message_id, user_id, user_name, text, created_at),
    )
    conn.close()

def count_stats():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chats;")
    groups = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT (chat_id, COALESCE(thread_id, -1))) FROM messages;")
    topics = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages;")
    msgs = cur.fetchone()[0]
    conn.close()
    return groups, topics, msgs

def list_groups():
    conn = db_conn()
    cur = conn.cursor()
    # pega apenas grupos/supergrupos que já tiveram mensagem capturada (ou foram vistos)
    cur.execute(
        """
        SELECT chat_id, COALESCE(title, CAST(chat_id AS TEXT)) AS title
        FROM chats
        WHERE chat_type IN ('group', 'supergroup')
        ORDER BY title ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [{"chat_id": int(r[0]), "title": r[1]} for r in rows]

def list_threads_for_group(chat_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(thread_id, -1) AS tid, COUNT(*) AS cnt, MAX(created_at) AS last_at
        FROM messages
        WHERE chat_id = %s
        GROUP BY COALESCE(thread_id, -1)
        ORDER BY last_at DESC
        """,
        (chat_id,),
    )
    rows = cur.fetchall()
    conn.close()

    # thread_id = -1 => General
    out = []
    for tid, cnt, last_at in rows:
        if int(tid) == -1:
            name = "#General"
            thread_id = None
        else:
            name = f"Tópico {int(tid)}"
            thread_id = int(tid)
        out.append({"thread_id": thread_id, "name": name, "count": int(cnt), "last_at": last_at})
    return out

def fetch_messages(chat_id: int, thread_id: int | None, day: date, limit: int = 400):
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)

    conn = db_conn()
    cur = conn.cursor()

    if thread_id is None:
        cur.execute(
            """
            SELECT created_at, COALESCE(user_name,'(sem nome)'), COALESCE(text,'')
            FROM messages
            WHERE chat_id = %s
              AND thread_id IS NULL
              AND created_at >= %s AND created_at < %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (chat_id, start, end, limit),
        )
    else:
        cur.execute(
            """
            SELECT created_at, COALESCE(user_name,'(sem nome)'), COALESCE(text,'')
            FROM messages
            WHERE chat_id = %s
              AND thread_id = %s
              AND created_at >= %s AND created_at < %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (chat_id, thread_id, start, end, limit),
        )

    rows = cur.fetchall()
    conn.close()
    return rows

# =========================
# OpenAI via requests (chat.completions)
# =========================
def openai_summarize(system: str, user: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4.1-mini",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    if r.status_code != 200:
        # retorna erro resumido pra você ver no Resumo RGL
        try:
            j = r.json()
        except Exception:
            j = {"error": r.text[:500]}
        raise RuntimeError(f"OpenAI erro ({r.status_code}): {j}")

    data = r.json()
    return data["choices"][0]["message"]["content"].strip()

# =========================
# UI state (por usuário)
# =========================
# context.user_data keys:
# - "step": "choose_group" | "choose_mode" | "choose_topic" | "choose_day" | "await_date"
# - "selected_group": int
# - "selected_thread": Optional[int]
# - "mode": "general" | "topic"
# - "awaiting": dict
def reset_flow(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

def is_in_target_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == TARGET_CHAT_ID)

# =========================
# Menus
# =========================
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗂️ Escolher grupo", callback_data="main:choose_group")],
        [InlineKeyboardButton("🔄 Atualizar lista de grupos", callback_data="main:refresh")],
    ])

def kb_groups(groups):
    rows = []
    for g in groups[:50]:  # evita teclado gigante
        rows.append([InlineKeyboardButton(g["title"], callback_data=f"group:{g['chat_id']}")])
    rows.append([InlineKeyboardButton("⬅️ Voltar", callback_data="main:back")])
    return InlineKeyboardMarkup(rows)

def kb_mode():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Resumo geral do grupo", callback_data="mode:general")],
        [InlineKeyboardButton("🧵 Resumo por tópico", callback_data="mode:topic")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="main:choose_group")],
    ])

def kb_topics(threads):
    rows = []
    for t in threads[:50]:
        label = t["name"]
        rows.append([InlineKeyboardButton(label, callback_data=f"topic:{'none' if t['thread_id'] is None else t['thread_id']}")])
    rows.append([InlineKeyboardButton("⬅️ Voltar", callback_data="mode:back")])
    return InlineKeyboardMarkup(rows)

def kb_day():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Hoje", callback_data="day:today")],
        [InlineKeyboardButton("📅 Ontem", callback_data="day:yesterday")],
        [InlineKeyboardButton("🗓️ Data específica", callback_data="day:pick")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="day:back")],
    ])

# =========================
# Handlers
# =========================
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # pode funcionar em qualquer chat (útil pra pegar IDs)
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    thread_id = update.effective_message.message_thread_id if update.effective_message else None
    await update.effective_message.reply_text(f"user_id: {user_id}\nchat_id: {chat_id}\nthread_id: {thread_id}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_in_target_chat(update):
        return
    groups, topics, msgs = count_stats()
    await update.effective_message.reply_text(
        f"📊 Status\n\nGrupos: {groups}\nTópicos: {topics}\nMensagens: {msgs}"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_in_target_chat(update):
        # não responde fora do Resumo RGL
        return

    reset_flow(context)
    # mostra status rápido
    groups, topics, msgs = count_stats()
    await update.effective_message.reply_text(
        f"Renan, estou 🟢 Online.\n\nStatus: {groups} grupos, {topics} tópicos, {msgs} mensagens.\n\nO que você quer fazer?",
        reply_markup=kb_main(),
    )

async def capture_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    CAPTURA mensagens de qualquer grupo/tópico.
    NÃO responde nada nesses grupos.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat:
        return

    # Só salva texto (pra começar simples)
    text = msg.text or msg.caption
    if not text:
        return

    # ignora comandos (pra não poluir)
    if text.startswith("/"):
        # exceto se for em grupo e for conversa útil? por enquanto ignora.
        return

    thread_id = msg.message_thread_id  # None se não for tópico
    created_at = msg.date
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc).astimezone(TZ)
    else:
        created_at = created_at.astimezone(TZ)

    user_id = user.id if user else None
    user_name = None
    if user:
        user_name = user.full_name or user.username or str(user.id)

    try:
        upsert_chat(chat.id, getattr(chat, "title", None), chat.type)
        save_message(chat.id, thread_id, msg.message_id, user_id, user_name, text, created_at)
    except Exception as e:
        log.exception("Erro salvando mensagem: %s", e)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_in_target_chat(update):
        return

    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""

    # MAIN
    if data == "main:back":
        reset_flow(context)
        await q.edit_message_text("O que você quer fazer?", reply_markup=kb_main())
        return

    if data == "main:refresh":
        groups, topics, msgs = count_stats()
        await q.edit_message_text(
            f"✅ Atualizei.\nStatus: {groups} grupos, {topics} tópicos, {msgs} mensagens.\n\nO que você quer fazer?",
            reply_markup=kb_main(),
        )
        return

    if data == "main:choose_group":
        gs = list_groups()
        if not gs:
            await q.edit_message_text(
                "Ainda não vi nenhum grupo.\n\n✅ Coloque o bot nos grupos e garanta que pessoas mandaram mensagens (texto).\nDepois clique em 🔄 Atualizar lista de grupos.",
                reply_markup=kb_main(),
            )
            return
        context.user_data["step"] = "choose_group"
        await q.edit_message_text("Escolha um grupo:", reply_markup=kb_groups(gs))
        return

    # GROUP selected
    if data.startswith("group:"):
        chat_id = int(data.split(":", 1)[1])
        context.user_data["selected_group"] = chat_id
        context.user_data["step"] = "choose_mode"
        await q.edit_message_text("Beleza. Quer resumo geral ou por tópico?", reply_markup=kb_mode())
        return

    # MODE
    if data == "mode:back":
        gs = list_groups()
        await q.edit_message_text("Escolha um grupo:", reply_markup=kb_groups(gs))
        return

    if data == "mode:general":
        context.user_data["mode"] = "general"
        context.user_data["selected_thread"] = None
        context.user_data["step"] = "choose_day"
        await q.edit_message_text("Qual dia do resumo?", reply_markup=kb_day())
        return

    if data == "mode:topic":
        context.user_data["mode"] = "topic"
        context.user_data["step"] = "choose_topic"
        chat_id = context.user_data.get("selected_group")
        threads = list_threads_for_group(chat_id)
        if not threads:
            await q.edit_message_text(
                "Ainda não tenho mensagens salvas nesse grupo.\nPeça pra alguém mandar uma mensagem de texto lá e tente de novo.",
                reply_markup=kb_mode(),
            )
            return
        await q.edit_message_text("Escolha um tópico:", reply_markup=kb_topics(threads))
        return

    # TOPIC selected
    if data.startswith("topic:"):
        raw = data.split(":", 1)[1]
        thread_id = None if raw == "none" else int(raw)
        context.user_data["selected_thread"] = thread_id
        context.user_data["step"] = "choose_day"
        await q.edit_message_text("Qual dia do resumo?", reply_markup=kb_day())
        return

    # DAY
    if data == "day:back":
        mode = context.user_data.get("mode")
        if mode == "topic":
            chat_id = context.user_data.get("selected_group")
            threads = list_threads_for_group(chat_id)
            await q.edit_message_text("Escolha um tópico:", reply_markup=kb_topics(threads))
        else:
            await q.edit_message_text("Quer resumo geral ou por tópico?", reply_markup=kb_mode())
        return

    if data in ("day:today", "day:yesterday"):
        day = datetime.now(tz=TZ).date()
        if data == "day:yesterday":
            day = day - timedelta(days=1)
        await q.edit_message_text("⏳ Gerando resumo… (vou enviar aqui no Resumo RGL)")
        await run_summary_flow(q, context, day)
        return

    if data == "day:pick":
        context.user_data["step"] = "await_date"
        await q.edit_message_text("Envie a data no formato **DD/MM/AAAA**.\nEx: 15/02/2026", parse_mode="Markdown")
        return

async def on_text_in_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Só para receber a data quando o usuário escolhe 'Data específica'
    (e somente no Resumo RGL).
    """
    if not is_in_target_chat(update):
        return

    step = context.user_data.get("step")
    if step != "await_date":
        return

    txt = (update.effective_message.text or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", txt)
    if not m:
        await update.effective_message.reply_text("Formato inválido. Envie assim: **DD/MM/AAAA** (ex: 15/02/2026).", parse_mode="Markdown")
        return

    dd, mm, yyyy = map(int, m.groups())
    try:
        day = date(yyyy, mm, dd)
    except Exception:
        await update.effective_message.reply_text("Data inválida. Tente de novo (ex: 15/02/2026).")
        return

    context.user_data["step"] = "idle"
    await update.effective_message.reply_text("⏳ Gerando resumo… (vou enviar aqui no Resumo RGL)")
    # cria um "fake" query-like pra reutilizar fluxo
    class DummyQ:
        async def edit_message_text(self, *args, **kwargs):  # não usado aqui
            pass
        async def message(self):  # não usado
            pass
    await run_summary_flow(update, context, day)

async def run_summary_flow(q_or_update, context: ContextTypes.DEFAULT_TYPE, day: date):
    chat_id = context.user_data.get("selected_group")
    thread_id = context.user_data.get("selected_thread")
    mode = context.user_data.get("mode")

    # descobre título do grupo
    groups = list_groups()
    group_title = next((g["title"] for g in groups if g["chat_id"] == chat_id), str(chat_id))

    # busca mensagens
    rows = fetch_messages(chat_id, thread_id, day, limit=400)

    if not rows:
        await send_to_target(context, f"❌ Não encontrei mensagens em **{day.strftime('%d/%m/%Y')}** nesse recorte.\n\nGrupo: *{group_title}*\nTópico: *{'#General' if thread_id is None else f'Tópico {thread_id}'}*", markdown=True)
        return

    # monta contexto pro resumo
    lines = []
    for created_at, user_name, text in rows:
        hhmm = created_at.astimezone(TZ).strftime("%H:%M")
        text = text.replace("\n", " ").strip()
        if len(text) > 300:
            text = text[:300] + "…"
        lines.append(f"{hhmm} — {user_name}: {text}")

    joined = "\n".join(lines)
    if len(joined) > 12000:
        # corta pra evitar prompt gigante
        joined = joined[-12000:]
        joined = "(Cortei mensagens antigas para caber no limite.)\n\n" + joined

    system = (
        "Você é um assistente que cria resumos objetivos de conversas de trabalho em grupos do Telegram.\n"
        "Regras:\n"
        "1) Responder em PT-BR.\n"
        "2) Fazer um resumo claro e útil.\n"
        "3) Incluir 'Quem falou o quê' (por pessoa) em bullets curtos.\n"
        "4) No fim, listar 'Pendências/ações' se existirem.\n"
        "5) Não inventar informações."
    )

    topic_label = "#General" if thread_id is None else f"Tópico {thread_id}"
    user_prompt = (
        f"Crie o resumo do dia {day.strftime('%d/%m/%Y')}.\n"
        f"Grupo: {group_title}\n"
        f"Recorte: {('Resumo geral do grupo' if mode=='general' else 'Resumo do tópico')} — {topic_label}\n\n"
        f"Mensagens:\n{joined}"
    )

    try:
        summary = openai_summarize(system=system, user=user_prompt)
    except Exception as e:
        await send_to_target(context, f"⚠️ Erro ao gerar resumo na OpenAI:\n`{e}`\n\n(Se aparecer 'insufficient_quota', precisa colocar crédito/plano na OpenAI.)", markdown=True)
        return

    header = f"🧾 *Resumo — {group_title}*\n📅 {day.strftime('%d/%m/%Y')} — {topic_label}\n\n"
    await send_to_target(context, header + summary, markdown=True)

async def send_to_target(context: ContextTypes.DEFAULT_TYPE, text: str, markdown: bool = False):
    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=text,
        parse_mode=("Markdown" if markdown else None),
        disable_web_page_preview=True,
    )

# =========================
# main
# =========================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # comandos
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("start", cmd_start))

    # callbacks do menu (somente Resumo RGL)
    app.add_handler(CallbackQueryHandler(on_callback))

    # captura mensagens em TODOS os grupos/tópicos (sem responder)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_all_messages))

    # captura data digitada no Resumo RGL
    app.add_handler(MessageHandler(filters.Chat(chat_id=TARGET_CHAT_ID) & filters.TEXT & ~filters.COMMAND, on_text_in_target))

    log.info("BOT rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
