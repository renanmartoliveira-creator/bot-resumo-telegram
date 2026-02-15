import os
import psycopg2
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from openai import OpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

def db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with db() as c:
        cur = c.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id BIGINT,
            topic_id BIGINT,
            user_name TEXT,
            text TEXT,
            created TIMESTAMP
        )
        """)
        c.commit()

def save_message(chat_id, topic_id, user, text):
    with db() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO messages VALUES (%s,%s,%s,%s,%s)",
            (chat_id, topic_id, user, text, datetime.utcnow())
        )
        c.commit()

def fetch_groups():
    with db() as c:
        cur = c.cursor()
        cur.execute("SELECT DISTINCT chat_id FROM messages")
        return [r[0] for r in cur.fetchall()]

def fetch_topics(chat_id):
    with db() as c:
        cur = c.cursor()
        cur.execute("SELECT DISTINCT topic_id FROM messages WHERE chat_id=%s", (chat_id,))
        return [r[0] for r in cur.fetchall()]

def count_all():
    with db() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(DISTINCT chat_id), COUNT(DISTINCT topic_id), COUNT(*) FROM messages")
        return cur.fetchone()

def fetch_messages(chat, topic, day):
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)

    with db() as c:
        cur = c.cursor()
        if topic is None:
            cur.execute("""
            SELECT user_name,text FROM messages
            WHERE chat_id=%s AND created BETWEEN %s AND %s
            """,(chat,start,end))
        else:
            cur.execute("""
            SELECT user_name,text FROM messages
            WHERE chat_id=%s AND topic_id=%s AND created BETWEEN %s AND %s
            """,(chat,topic,start,end))
        return cur.fetchall()

async def capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group","supergroup"]:
        save_message(
            update.effective_chat.id,
            update.message.message_thread_id,
            update.effective_user.full_name,
            update.message.text or ""
        )

async def cmd_id(update:Update,ctx):
    await update.message.reply_text(
        f"user_id: {update.effective_user.id}\nchat_id: {update.effective_chat.id}\nthread_id: {update.message.message_thread_id}"
    )

async def cmd_status(update:Update,ctx):
    g,t,m = count_all()
    await update.message.reply_text(f"📊 Status\n\nGrupos: {g}\nTópicos: {t}\nMensagens: {m}")

async def start(update:Update,ctx):
    if update.effective_chat.id!=TARGET_CHAT_ID: return
    if update.effective_user.id!=ADMIN_USER_ID: return
    await show_groups(update,ctx)

async def show_groups(update,ctx):
    groups = fetch_groups()
    buttons=[]
    for g in groups:
        buttons.append([InlineKeyboardButton(str(g),callback_data=f"grp:{g}")])
    buttons.append([InlineKeyboardButton("🔄 Atualizar lista de grupos",callback_data="refresh")])
    await update.effective_chat.send_message("Escolha um grupo:",reply_markup=InlineKeyboardMarkup(buttons))

async def callbacks(update:Update,ctx):
    q=update.callback_query
    await q.answer()

    if q.data=="refresh":
        await show_groups(update,ctx)
        return

    if q.data.startswith("grp:"):
        chat=int(q.data.split(":")[1])
        ctx.user_data["chat"]=chat
        topics=fetch_topics(chat)
        btn=[]
        btn.append([InlineKeyboardButton("📋 Todos tópicos",callback_data="topic:all")])
        for t in topics:
            btn.append([InlineKeyboardButton(str(t),callback_data=f"topic:{t}")])
        await q.message.edit_text("Escolha tópico:",reply_markup=InlineKeyboardMarkup(btn))

    if q.data.startswith("topic:"):
        val=q.data.split(":")[1]
        ctx.user_data["topic"]=None if val=="all" else int(val)
        btn=[
            [InlineKeyboardButton("📅 Hoje",callback_data="day:0")],
            [InlineKeyboardButton("📅 Ontem",callback_data="day:1")]
        ]
        await q.message.edit_text("Escolha data:",reply_markup=InlineKeyboardMarkup(btn))

    if q.data.startswith("day:"):
        delta=int(q.data.split(":")[1])
        day=datetime.utcnow().date()-timedelta(days=delta)
        chat=ctx.user_data["chat"]
        topic=ctx.user_data["topic"]

        rows=fetch_messages(chat,topic,day)
        if not rows:
            await q.message.reply_text("Nada encontrado.")
            return

        txt="\n".join([f"{u}: {t}" for u,t in rows])

        prompt=f"Resuma objetivamente:\n{txt}"

        resp=client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}]
        )

        await q.message.reply_text(resp.choices[0].message.content)

async def main():
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("status",cmd_status))
    app.add_handler(CommandHandler("id",cmd_id))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,capture))

    app.run_polling()

if __name__=="__main__":
    main()
