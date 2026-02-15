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
from openai import RateLimitError, APIStatusError, APIConnectionError

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))          # chat_id do "Resumo RGL"
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))            # seu user_id (opcional p/ admin)
DB_PATH = os.getenv("DB_PATH", "/data/data.db")                 # <-- use Volume /data no Railway
TZ = timezone(timedelta(hours=-3))

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN nas variáveis do Railway.")
if not OPENAI_API_KEY:
    raise RuntimeError("Defina OPENAI_API_KEY nas variáveis do Railway.")
if TARGET_CHAT_ID == 0:
    raise RuntimeError("Defina TARGET_CHAT_ID nas variáveis do Railway.")

client = OpenAI(api_key=OPENAI_API_KEY)

# ====== DB ======
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)

def init_db():
    c = db()
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        chat_id INTEGER,
        thread_id INTEGER,
        thread_name TEXT,
        user_name TEXT,
        text TEXT,
        created TEXT
    )
    """)
    c.commit()
    c.close()

def save_msg(chat_id: int, thread_id: int | None, thread_name: str, user_name: str, text: str):
    c = db()
    c.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        (chat_id, thread_id, thread_name, user_name, text, datetime.now(TZ).isoformat()),
    )
    c.commit()
    c.close()

# ====== CAPTURA (tudo que falarem nos grupos) ======
async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.message.chat
    if chat.type not in ("group", "supergroup"):
        return

    # Nome do grupo
    group_name = chat.title or "Grupo sem nome"

    # Tópicos (Forum): message_thread_id pode existir
    thread_id = getattr(update.message, "message_thread_id", None)

    # Nome do tópico: no Telegram nem sempre vem no update.
    # Então guardamos como "Grupo" quando não conseguimos o nome real do tópico.
    # (Depois podemos melhorar para capturar títulos de tópico quando disponíveis.)
    thread_name = group_name if thread_id is None else f"{group_name} (tópico {thread_id})"

    user_name = update.message.from_user.full_name if update.message.from_user else "SemNome"
    text = update.message.text or ""
    if not text.strip():
        return

    save_msg(chat.id, thread_id, thread_name, user_name, text)

# ====== /id ======
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TARGET_CHAT_ID:
        return
    await update.message.reply_text(
        f"user_id: {update.effective_user.id}\n"
        f"chat_id: {update.effective_chat.id}\n"
        f"thread_id: {getattr(update.message, 'message_thread_id', None)}"
    )

# ====== /status ======
async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TARGET_CHAT_ID:
        return

    c = db()
    groups = c.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0]
    topics = c.execute("SELECT COUNT(DISTINCT thread_id) FROM messages").fetchone()[0]
    msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    c.close()

    await update.message.reply_text(
        f"📊 Status\n\nGrupos: {groups}\nTópicos: {topics}\nMensagens: {msgs}"
    )

# ====== MENU: listar grupos (por chat_id) ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TARGET_CHAT_ID:
        return

    c = db()
    rows = c.execute("SELECT DISTINCT chat_id FROM messages ORDER BY chat_id DESC").fetchall()
    c.close()

    if not rows:
        await update.message.reply_text(
            "Ainda não vi nenhum grupo.\n"
            "Coloque o bot nos grupos e aguarde mensagens novas.\n\n"
            "Dica: mande uma mensagem em qualquer tópico do grupo, só pra ele registrar."
        )
        return

    buttons = []
    for (chat_id,) in rows:
        # Mostra chat_id mesmo (pra ficar garantido). Depois podemos buscar nome via Bot API.
        buttons.append([InlineKeyboardButton(f"Grupo {chat_id}", callback_data=f"G|{chat_id}")])

    buttons.append([InlineKeyboardButton("🔄 Atualizar lista de grupos", callback_data="REFRESH")])

    await update.message.reply_text(
        "Escolha um grupo:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# ====== GERAR RESUMO via OpenAI ======
def build_prompt(lines: list[str]) -> str:
    joined = "\n".join(lines)
    return (
        "Você é um assistente de gestão.\n"
        "Resuma as mensagens abaixo do dia selecionado.\n"
        "Quero:\n"
        "1) Resumo geral\n"
        "2) Quem falou o quê (por pessoa)\n"
        "3) Reclamações / problemas\n"
        "4) Ações sugeridas / o que melhorar\n"
        "Seja objetivo e organizado.\n\n"
        f"Mensagens:\n{joined}"
    )

async def run_openai(prompt: str) -> str:
    # SDK novo: chat.completions
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()

# ====== CALLBACKS ======
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.message.chat_id != TARGET_CHAT_ID:
        return

    data = q.data

    if data == "REFRESH":
        await q.message.reply_text("Atualizei. Use /start novamente.")
        return

    # Selecionou grupo
    if data.startswith("G|"):
        chat_id = int(data.split("|", 1)[1])

        # Pega mensagens de HOJE (BRT)
        start_day = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = start_day + timedelta(days=1)

        c = db()
        rows = c.execute(
            "SELECT user_name, text FROM messages WHERE chat_id=? AND created>=? AND created<? ORDER BY created ASC",
            (chat_id, start_day.isoformat(), end_day.isoformat()),
        ).fetchall()
        c.close()

        if not rows:
            await q.message.reply_text("Não encontrei mensagens hoje nesse grupo.")
            return

        lines = [f"{u}: {t}" for u, t in rows]
        prompt = build_prompt(lines)

        await q.message.reply_text("Gerando resumo...")

        try:
            out = await run_openai(prompt)
        except RateLimitError:
            await q.message.reply_text(
                "❌ A OpenAI recusou por **quota insuficiente**.\n"
                "Isso acontece quando a conta/projeto está sem crédito/billing.\n"
                "Ative o pagamento ou adicione créditos e tente de novo."
            )
            return
        except (APIConnectionError, APIStatusError) as e:
            await q.message.reply_text(f"❌ Erro ao chamar OpenAI: {type(e).__name__}")
            return
        except Exception as e:
            await q.message.reply_text(f"❌ Erro inesperado: {type(e).__name__}")
            return

        await ctx.bot.send_message(chat_id=TARGET_CHAT_ID, text=out)
        return

# ====== MAIN ======
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # comandos só funcionam no Resumo RGL
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("id", cmd_id))

    # botões
    app.add_handler(CallbackQueryHandler(on_callback))

    # captura global em grupos (não manda nada nos grupos)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()

if __name__ == "__main__":
    main()
