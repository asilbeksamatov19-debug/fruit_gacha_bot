import logging
import os
import sqlite3
import random
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = "bot.db"

# GitHub'dagi fruits.py dagi ro'yxat saqlangan. Dragon uchun foydalanuvchi so'ragan
# juda kichik qiymat alohida qo'yilgan.
FRUITS = [
    ("Phoenix", 0.4048582996), ("Spirit", 0.0506072874), ("Portal", 0.3542510121),
    ("Bomb", 8.6032388664), ("Spike", 6.5789473684), ("Venom", 0.0708502024),
    ("T-Rex", 0.1012145749), ("Eagle", 2.5303643725), ("Creation", 0.5060728745),
    ("Kitsune", 0.020242915), ("Rocket", 13.1578947368), ("Pain", 0.2226720648),
    ("Ice", 4.5546558704), ("Smoke", 7.5910931174), ("Rubber", 1.5182186235),
    ("Dragon", 0.00005), ("Buddha", 0.3036437247), ("Dough", 0.0910931174),
    ("Blade", 10.1214574899), ("Flame", 5.0607287449), ("Magma", 1.1133603239),
    ("Light", 1.8218623482), ("Diamond", 2.024291498), ("Blizzard", 0.2024291498),
    ("Shadow", 0.0809716599), ("Spider", 0.7085020243), ("Mammoth", 0.1214574899),
    ("Dark", 3.036437247), ("Sand", 3.5425101215), ("Quake", 0.9109311741),
    ("Tiger", 0.04048583), ("Gas", 0.0607287449), ("Spin", 12.1457489879),
    ("Gravity", 0.1518218623), ("Ghost", 1.2145748988), ("Control", 0.0151821862),
    ("Yeti", 0.0303643725), ("Sound", 0.455465587), ("Lightning", 0.2530364372),
    ("Love", 0.6072874494), ("Spring", 9.6153846154),
]

async def error_handler(update, context):
    logging.exception("Telegram botda kutilmagan xato:", exc_info=context.error)


def db():
    con = sqlite3.connect(DB_NAME, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        name TEXT DEFAULT '',
        tickets INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS user_fruits (
        user_id INTEGER NOT NULL,
        fruit_name TEXT NOT NULL,
        amount INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, fruit_name)
    );
    CREATE TABLE IF NOT EXISTS bot_groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1,
        gacha_enabled INTEGER DEFAULT 1,
        ticket_amount INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS group_users (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tickets INTEGER DEFAULT 0,
        last_gacha INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS required_chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        title TEXT DEFAULT '',
        chat_type TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1,
        UNIQUE(group_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS ticket_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        friend_id INTEGER,
        group_id INTEGER,
        status TEXT DEFAULT 'pending',
        created_at INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS fruits (
        name TEXT PRIMARY KEY,
        chance REAL NOT NULL,
        value REAL DEFAULT 0,
        enabled INTEGER DEFAULT 1
    );
    """)
    # user_fruits: migrate old global Store to group-based Store.
    uf_cols = {r[1] for r in cur.execute("PRAGMA table_info(user_fruits)").fetchall()}
    if "group_id" not in uf_cols:
        cur.execute("ALTER TABLE user_fruits RENAME TO user_fruits_old")
        cur.execute("""
            CREATE TABLE user_fruits (
                group_id INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER NOT NULL,
                fruit_name TEXT NOT NULL,
                amount INTEGER DEFAULT 0,
                PRIMARY KEY (group_id, user_id, fruit_name)
            )
        """)
        cur.execute("""
            INSERT INTO user_fruits (group_id,user_id,fruit_name,amount)
            SELECT 0,user_id,fruit_name,amount FROM user_fruits_old
        """)
        cur.execute("DROP TABLE user_fruits_old")

    # Old database compatibility: add missing columns if an older version exists.
    cols = {r[1] for r in cur.execute("PRAGMA table_info(ticket_requests)").fetchall()}
    if "group_id" not in cols:
        cur.execute("ALTER TABLE ticket_requests ADD COLUMN group_id INTEGER")
    cols = {r[1] for r in cur.execute("PRAGMA table_info(required_chats)").fetchall()}
    if "group_id" not in cols:
        cur.execute("ALTER TABLE required_chats ADD COLUMN group_id INTEGER DEFAULT 0")
    cols = {r[1] for r in cur.execute("PRAGMA table_info(bot_groups)").fetchall()}
    if "gacha_enabled" not in cols:
        cur.execute("ALTER TABLE bot_groups ADD COLUMN gacha_enabled INTEGER DEFAULT 1")
    if "ticket_amount" not in cols:
        cur.execute("ALTER TABLE bot_groups ADD COLUMN ticket_amount INTEGER DEFAULT 1")
    if "enabled" not in cols:
        cur.execute("ALTER TABLE bot_groups ADD COLUMN enabled INTEGER DEFAULT 1")

    for key, value in (("global_bot_enabled", "1"),):
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
    for name, chance in FRUITS:
        cur.execute(
            "INSERT INTO fruits(name,chance,value,enabled) VALUES(?,?,0,1) "
            "ON CONFLICT(name) DO UPDATE SET chance=excluded.chance",
            (name, chance),
        )
    con.commit()
    con.close()


def save_user(user):
    if not user:
        return
    con = db()
    con.execute("""
        INSERT INTO users(user_id,username,name) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,name=excluded.name
    """, (user.id, user.username or "", user.full_name or ""))
    con.commit()
    con.close()


def ensure_group(chat):
    if not chat or chat.type not in ("group", "supergroup"):
        return
    con = db()
    con.execute("""
        INSERT INTO bot_groups(chat_id,title) VALUES(?,?)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
    """, (chat.id, chat.title or str(chat.id)))
    con.commit()
    con.close()


def ensure_group_user(chat_id, user_id):
    con = db()
    con.execute("INSERT OR IGNORE INTO group_users(chat_id,user_id,tickets) VALUES(?,?,0)", (chat_id, user_id))
    con.commit()
    con.close()


def group_row(chat_id):
    con = db()
    row = con.execute("SELECT chat_id,title,enabled,gacha_enabled,ticket_amount FROM bot_groups WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return row


def is_owner(user_id):
    return user_id == OWNER_ID


async def is_group_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return False
    if user.id == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def main_menu(chat_id=None):
    gid = chat_id if chat_id is not None else 0
    buttons = [
        [InlineKeyboardButton("🎫 Ticket", callback_data=f"panel:ticket:{gid}")],
        [InlineKeyboardButton("📢 Kanal/Guruh", callback_data=f"panel:chats:{gid}")],
        [InlineKeyboardButton("🎰 Gacha ON/OFF", callback_data=f"panel:gacha:{gid}")],
        [InlineKeyboardButton("🤖 Bot ON/OFF", callback_data=f"panel:bot:{gid}")],
        [InlineKeyboardButton("🍎 Fruits", callback_data=f"panel:fruits:{gid}")],
        [InlineKeyboardButton("🏪 Store", callback_data=f"panel:store:{gid}")],
    ]
    return InlineKeyboardMarkup(buttons)

def group_panel_text(chat_id):
    row = group_row(chat_id)
    if not row:
        return "⚙️ GROUP PANEL"
    _, title, enabled, gacha_enabled, ticket_amount = row
    return (
        f"⚙️ GROUP PANEL\n\n"
        f"📌 {title}\n"
        f"🤖 Bot: {'🟢 ON' if enabled else '🔴 OFF'}\n"
        f"🎰 Gacha: {'🟢 ON' if gacha_enabled else '🔴 OFF'}\n"
        f"🎫 Ticket: {ticket_amount} ta"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        ensure_group(chat)
        ensure_group_user(chat.id, update.effective_user.id)
    args = context.args or []
    if args and args[0].startswith("ticket_"):
        try:
            request_id = int(args[0].split("_", 1)[1])
        except ValueError:
            request_id = 0
        if request_id:
            await open_ticket_request(update, context, request_id)
            return
    await update.message.reply_text(
        "🍎 Blox Fruits Gacha Bot\n\n"
        "🎰 /gacha — Gacha o‘ynash\n"
        "🏪 /store — Store\n"
        "🎫 /ticket — Ticket olish\n"
        "⚙️ /admin — Admin panel"
    )


async def open_ticket_request(update, context, request_id):
    friend_id = update.effective_user.id
    con = db()
    req = con.execute("SELECT owner_id,friend_id,group_id,status FROM ticket_requests WHERE id=?", (request_id,)).fetchone()
    if not req:
        con.close()
        await update.message.reply_text("❌ Ticket so‘rovi topilmadi.")
        return
    owner_id, saved_friend, group_id, status = req
    if status != "pending":
        con.close()
        await update.message.reply_text("ℹ️ Bu ticket so‘rovi allaqachon ishlatilgan.")
        return
    if owner_id == friend_id:
        con.close()
        await update.message.reply_text("❌ O‘zingizning ticket linkingizni o‘zingiz tasdiqlay olmaysiz.")
        return
    if saved_friend and saved_friend != friend_id:
        con.close()
        await update.message.reply_text("❌ Bu link boshqa foydalanuvchi uchun ochilgan.")
        return
    con.execute("UPDATE ticket_requests SET friend_id=? WHERE id=? AND status='pending'", (friend_id, request_id))
    chats = con.execute(
        "SELECT id,chat_id,title FROM required_chats WHERE group_id=? AND enabled=1 ORDER BY id", (group_id or 0,)
    ).fetchall()
    con.commit()
    con.close()
    keyboard = []
    for row_id, chat_id, title in chats:
        keyboard.append([InlineKeyboardButton(f"📢 {title}", callback_data=f"ticket:join:{row_id}")])
    keyboard.append([InlineKeyboardButton("✅ Tekshirish", callback_data=f"ticket:verify:{request_id}")])
    text = "🎫 Ticket olish\n\nQuyidagi kanal/guruh(lar)ga qo‘shiling:"
    if not chats:
        text = "🎫 Ticket olish\n\nBu guruhda majburiy kanal/guruh sozlanmagan.\nTekshirishni bosing."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def gacha(update, context):
    save_user(update.effective_user)
    chat = update.effective_chat
    ensure_group(chat) if chat and chat.type in ("group", "supergroup") else None
    user_id = update.effective_user.id
    group_id = chat.id if chat and chat.type in ("group", "supergroup") else None
    con = db()
    if group_id is not None:
        ensure_group_user(group_id, user_id)
        row = con.execute("SELECT enabled,gacha_enabled,tickets FROM bot_groups JOIN group_users ON group_users.chat_id=bot_groups.chat_id WHERE bot_groups.chat_id=? AND group_users.user_id=?", (group_id,user_id)).fetchone()
        if not row or not row[0]:
            con.close(); await update.message.reply_text("🔴 Bu guruhda bot o‘chirilgan."); return
        if not row[1]:
            con.close(); await update.message.reply_text("🔴 Bu guruhda Gacha hozircha o‘chirilgan."); return
        tickets = row[2]
    else:
        global_on = con.execute("SELECT value FROM settings WHERE key='global_bot_enabled'").fetchone()
        if global_on and int(global_on[0]) == 0:
            con.close(); await update.message.reply_text("🔴 Bot hozircha o‘chirilgan."); return
        tickets = con.execute("SELECT tickets FROM users WHERE user_id=?", (user_id,)).fetchone()
        tickets = tickets[0] if tickets else 0
    if tickets <= 0:
        con.close()
        await update.message.reply_text("❌ Sizda ticket yo‘q!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎫 Ticket olish", callback_data="ticket:get")]]))
        return
    fruits = con.execute("SELECT name,chance FROM fruits WHERE enabled=1 AND chance>0").fetchall()
    if not fruits:
        con.close(); await update.message.reply_text("❌ Aktiv fruit yo‘q."); return
    fruit_name = random.choices([x[0] for x in fruits], weights=[x[1] for x in fruits], k=1)[0]
    chance = next(x[1] for x in fruits if x[0] == fruit_name)
    if group_id is not None:
        con.execute("UPDATE group_users SET tickets=tickets-1,last_gacha=? WHERE chat_id=? AND user_id=?", (int(time.time()),group_id,user_id))
    else:
        con.execute("UPDATE users SET tickets=tickets-1 WHERE user_id=?", (user_id,))
    con.execute("INSERT INTO user_fruits(group_id,user_id,fruit_name,amount) VALUES(?,?,?,1) ON CONFLICT(group_id,user_id,fruit_name) DO UPDATE SET amount=amount+1", (group_id or 0,user_id,fruit_name))
    con.commit(); con.close()
    image_path = "fruit_images/Dragon_East.png" if fruit_name == "Dragon" else f"fruit_images/{fruit_name}_Fruit.png"
    caption = f"🍎 {fruit_name}\n🎲 Tushish shansi: {chance:.10f}%"
    if os.path.exists(image_path):
        with open(image_path,"rb") as photo:
            await update.message.reply_photo(photo=photo,caption=caption)
    else:
        await update.message.reply_text(caption)


async def store(update, context):
    save_user(update.effective_user)
    target_id = update.effective_user.id
    title = "🏪 Sizning Store"
    if context.args:
        username = context.args[0].lstrip("@")
        con = db(); row = con.execute("SELECT user_id,name,username FROM users WHERE lower(username)=lower(?)", (username,)).fetchone(); con.close()
        if not row:
            await update.message.reply_text("❌ Bu foydalanuvchi topilmadi."); return
        target_id = row[0]; title = f"🏪 @{row[2]}" if row[2] else f"🏪 {row[1]}"
    chat = update.effective_chat
    group_id = chat.id if chat and chat.type in ("group","supergroup") else 0
    con = db(); fruits = con.execute("SELECT fruit_name,amount FROM user_fruits WHERE group_id=? AND user_id=? AND amount>0 ORDER BY fruit_name", (group_id,target_id)).fetchall(); con.close()
    if not fruits:
        await update.message.reply_text(f"{title}\n\n📦 Store hozircha bo‘sh."); return
    await update.message.reply_text(title + "\n\n" + "\n".join(f"🍎 {name} × {amount}" for name,amount in fruits))


async def ticket_command(update, context):
    save_user(update.effective_user)
    chat = update.effective_chat
    group_id = chat.id if chat and chat.type in ("group","supergroup") else None
    if group_id is not None:
        ensure_group(chat); ensure_group_user(group_id, update.effective_user.id)
    con = db()
    cur = con.execute("INSERT INTO ticket_requests(owner_id,group_id,status,created_at) VALUES(?,?,?,?)", (update.effective_user.id,group_id,"pending",int(time.time())))
    request_id = cur.lastrowid; con.commit(); con.close()
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ticket_{request_id}"
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎫 Menga ticket olishga yordam ber!')}"
    await update.message.reply_text("🎫 Ticket olish uchun linkni do‘stingizga yuboring:\n\n" + link, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 Do‘stga yuborish",url=share)]]))


async def addchat(update, context):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ /addchat faqat guruh ichida ishlaydi.")
        return
    if not await is_group_admin(update, context):
        await update.message.reply_text("❌ Faqat guruh admini ishlata oladi.")
        return
    ensure_group(chat)
    if not context.args:
        await update.message.reply_text("📢 Kanal/guruh ID sini yozing.\nMasalan: /addchat -1001234567890")
        return
    try:
        cid = int(context.args[0])
        target = await context.bot.get_chat(cid)
        if target.type not in ("channel", "group", "supergroup"):
            await update.message.reply_text("❌ Faqat kanal yoki guruh ID sini kiriting.")
            return
        con = db()
        con.execute("INSERT INTO required_chats(group_id,chat_id,title,chat_type,enabled) VALUES(?,?,?,?,1) ON CONFLICT(group_id,chat_id) DO UPDATE SET title=excluded.title,chat_type=excluded.chat_type,enabled=1", (chat.id,str(target.id),target.title or str(target.id),target.type))
        con.commit(); con.close()
        await update.message.reply_text(f"✅ {target.title or target.id} shu guruhga qo‘shildi.")
    except ValueError:
        await update.message.reply_text("❌ Chat ID noto‘g‘ri.")
    except Exception:
        await update.message.reply_text("❌ Chatni topib bo‘lmadi. Botning o‘sha kanal/guruhdagi huquqlarini tekshiring.")


async def admin(update, context):
    chat = update.effective_chat
    if chat and chat.type in ("group","supergroup"):
        if not await is_group_admin(update,context):
            await update.message.reply_text("❌ Faqat guruh admini ishlata oladi."); return
        ensure_group(chat)
        await update.message.reply_text(group_panel_text(chat.id), reply_markup=main_menu(chat.id))
        return
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Siz admin emassiz."); return
    con=db(); groups=con.execute("SELECT chat_id,title FROM bot_groups ORDER BY title").fetchall(); con.close()
    if not groups:
        await update.message.reply_text("📭 Hali birorta guruh botga yozmagan.")
        return
    kb=[[InlineKeyboardButton(f"👥 {title}",callback_data=f"select_group:{gid}")] for gid,title in groups]
    await update.message.reply_text("⚙️ ADMIN\n\nQaysi guruh panelini ochamiz?",reply_markup=InlineKeyboardMarkup(kb))


async def panel_callback(update, context):
    q=update.callback_query; await q.answer(); data=q.data
    if data.startswith("select_group:"):
        gid=int(data.split(":",1)[1])
        if not is_owner(q.from_user.id): return
        context.user_data["target_group_id"]=gid
        await q.message.reply_text(group_panel_text(gid),reply_markup=main_menu(gid)); return
    if data == "ticket:get":
        await create_ticket_from_callback(update,context); return
    if data.startswith("ticket:join:"):
        await ticket_join(update,context,int(data.split(":")[-1])); return
    if data.startswith("ticket:verify:"):
        await ticket_verify(update,context,int(data.split(":")[-1])); return
    if not data.startswith("panel:"): return
    parts=data.split(":")
    action=parts[1]
    group_id=int(parts[2]) if len(parts) > 2 and parts[2] else q.message.chat.id
    if group_id == 0:
        group_id=context.user_data.get("target_group_id", q.message.chat.id)
    con=db(); exists=con.execute("SELECT 1 FROM bot_groups WHERE chat_id=?",(group_id,)).fetchone(); con.close()
    if not exists: return
    if q.message.chat.type != "private" and not await is_group_admin(update,context):
        await q.message.reply_text("❌ Faqat guruh admini."); return
    if q.message.chat.type == "private" and not is_owner(q.from_user.id): return
    if not exists: return
    if q.message.chat.type != "private" and not await is_group_admin(update,context):
        await q.message.reply_text("❌ Faqat guruh admini."); return
    if q.message.chat.type == "private" and not is_owner(q.from_user.id): return
    if action=="bot":
        con=db(); row=con.execute("SELECT enabled FROM bot_groups WHERE chat_id=?",(group_id,)).fetchone(); new=0 if row[0] else 1; con.execute("UPDATE bot_groups SET enabled=? WHERE chat_id=?",(new,group_id)); con.commit(); con.close()
        await q.message.reply_text(group_panel_text(group_id),reply_markup=main_menu(group_id)); return
    if action=="gacha":
        con=db(); row=con.execute("SELECT gacha_enabled FROM bot_groups WHERE chat_id=?",(group_id,)).fetchone(); new=0 if row[0] else 1; con.execute("UPDATE bot_groups SET gacha_enabled=? WHERE chat_id=?",(new,group_id)); con.commit(); con.close()
        await q.message.reply_text(group_panel_text(group_id),reply_markup=main_menu(group_id)); return
    if action=="ticket":
        con=db(); row=con.execute("SELECT ticket_amount FROM bot_groups WHERE chat_id=?",(group_id,)).fetchone(); amount=row[0]; con.close()
        kb=[[InlineKeyboardButton(f"🎫 Ticket soni: {amount}",callback_data=f"set_ticket:{group_id}")],[InlineKeyboardButton("👤 Bitta userga ticket",callback_data=f"give_one:{group_id}")],[InlineKeyboardButton("👥 Hammaga ticket",callback_data=f"give_all:{group_id}")],[InlineKeyboardButton("♻️ Hammaga ticket=0",callback_data=f"reset_tickets:{group_id}")],[InlineKeyboardButton("🔙 Panel",callback_data=f"back:{group_id}")]]
        context.user_data["target_group_id"]=group_id
        await q.message.reply_text("🎫 TICKET SOZLAMALARI",reply_markup=InlineKeyboardMarkup(kb)); return
    if action=="chats":
        await show_required_chats(q.message,group_id); context.user_data["target_group_id"]=group_id; return
    if action=="store":
        kb=[[InlineKeyboardButton("♻️ Store = 0 (hamma)",callback_data=f"clear_store:{group_id}")],[InlineKeyboardButton("🔙 Panel",callback_data=f"back:{group_id}")]]
        context.user_data["target_group_id"]=group_id; await q.message.reply_text("🏪 STORE SOZLAMALARI",reply_markup=InlineKeyboardMarkup(kb)); return
    if action=="fruits":
        con=db(); rows=con.execute("SELECT name,chance FROM fruits WHERE enabled=1 ORDER BY chance ASC").fetchall(); con.close()
        text="🍎 AKTIV FRUITS\n\n"+"\n".join(f"{name} — {chance:.10f}%" for name,chance in rows)
        await q.message.reply_text(text[:4000]); return


async def create_ticket_from_callback(update,context):
    q=update.callback_query; chat=q.message.chat
    group_id=chat.id if chat.type in ("group","supergroup") else context.user_data.get("target_group_id")
    con=db(); cur=con.execute("INSERT INTO ticket_requests(owner_id,group_id,status,created_at) VALUES(?,?,?,?)",(q.from_user.id,group_id,"pending",int(time.time()))); rid=cur.lastrowid; con.commit(); con.close()
    me=await context.bot.get_me(); link=f"https://t.me/{me.username}?start=ticket_{rid}"; share=f"https://t.me/share/url?url={quote(link)}&text={quote('🎫 Menga ticket olishga yordam ber!')}"
    await q.message.reply_text(f"🎫 Ticket link:\n\n{link}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 Do‘stga yuborish",url=share)]]))


async def show_required_chats(message,group_id):
    con=db(); rows=con.execute("SELECT id,title,chat_id,chat_type,enabled FROM required_chats WHERE group_id=? ORDER BY id",(group_id,)).fetchall(); con.close()
    text="📢 MAJBURIY KANAL/GURUHLAR\n\n"
    if rows:
        for rid,title,cid,ctype,en in rows:
            text += f"#{rid} — {title}\n🆔 {cid} | {ctype}\n{'🟢 ON' if en else '🔴 OFF'}\n\n"
    else: text += "📭 Hali qo‘shilmagan.\n"
    kb=[[InlineKeyboardButton("➕ Qo‘shish",callback_data=f"add_chat:{group_id}")],[InlineKeyboardButton("🔄 ON/OFF",callback_data=f"toggle_chat:{group_id}")],[InlineKeyboardButton("➖ O‘chirish",callback_data=f"remove_chat:{group_id}")],[InlineKeyboardButton("🔙 Panel",callback_data=f"back:{group_id}")]]
    await message.reply_text(text,reply_markup=InlineKeyboardMarkup(kb))


async def ticket_join(update,context,row_id):
    q=update.callback_query
    con=db(); row=con.execute("SELECT chat_id,title FROM required_chats WHERE id=? AND enabled=1",(row_id,)).fetchone(); con.close()
    if not row: await q.message.reply_text("❌ Chat topilmadi."); return
    cid,title=row
    try:
        chat=await context.bot.get_chat(cid)
        url=f"https://t.me/{chat.username}" if chat.username else (await context.bot.create_chat_invite_link(cid)).invite_link
        await q.message.reply_text(f"📢 {title}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Qo‘shilish",url=url)]]))
    except Exception:
        await q.message.reply_text("❌ Bu chat uchun link olishda xatolik. Bot admin ekanini tekshiring.")


async def ticket_verify(update,context,request_id):
    q=update.callback_query; friend=q.from_user.id
    con=db(); req=con.execute("SELECT owner_id,friend_id,group_id,status FROM ticket_requests WHERE id=?",(request_id,)).fetchone(); con.close()
    if not req: await q.message.reply_text("❌ So‘rov topilmadi."); return
    owner,saved_friend,group_id,status=req
    if status!="pending": await q.message.reply_text("ℹ️ Bu so‘rov ishlatilgan."); return
    if saved_friend and saved_friend!=friend: await q.message.reply_text("❌ Bu link boshqa user uchun."); return
    con=db(); chats=con.execute("SELECT chat_id,title FROM required_chats WHERE group_id=? AND enabled=1",(group_id or 0,)).fetchall(); con.close()
    missing=[]
    for cid,title in chats:
        try:
            m=await context.bot.get_chat_member(cid,friend); joined=m.status in ("member","administrator","creator") or (m.status=="restricted" and getattr(m,"is_member",False))
            if not joined: missing.append(title)
        except Exception: missing.append(title)
    if missing:
        await q.message.reply_text("❌ Hali qo‘shilmagan:\n\n"+"\n".join("• "+x for x in missing)); return
    con=db(); req2=con.execute("SELECT status FROM ticket_requests WHERE id=?",(request_id,)).fetchone()
    if not req2 or req2[0]!="pending": con.close(); await q.message.reply_text("ℹ️ So‘rov ishlatilgan."); return
    if group_id is not None:
        con.execute("INSERT INTO group_users(chat_id,user_id,tickets) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET tickets=tickets+1",(group_id,owner))
    else:
        con.execute("UPDATE users SET tickets=tickets+1 WHERE user_id=?",(owner,))
    con.execute("UPDATE ticket_requests SET friend_id=?,status='completed' WHERE id=? AND status='pending'",(friend,request_id)); con.commit(); con.close()
    await q.message.reply_text("✅ Tekshiruv muvaffaqiyatli! Ticket egasiga berildi.")
    try: await context.bot.send_message(owner,"🎉 Do‘stingiz tekshiruvdan o‘tdi!\n🎫 Sizga ticket berildi.")
    except Exception: pass


async def action_callback(update,context):
    q=update.callback_query; data=q.data
    if not any(data.startswith(x) for x in ("set_ticket:","give_one:","give_all:","reset_tickets:","back:","clear_store:","add_chat:","toggle_chat:","remove_chat:")): return
    await q.answer()
    gid=int(data.split(":",1)[1]); context.user_data["target_group_id"]=gid
    if not is_owner(q.from_user.id):
        try:
            m=await context.bot.get_chat_member(gid,q.from_user.id)
            if m.status not in ("administrator","creator"): await q.message.reply_text("❌ Faqat guruh admini."); return
        except Exception: return
    if data.startswith("back:"):
        await q.message.reply_text(group_panel_text(gid),reply_markup=main_menu(gid)); return
    if data.startswith("set_ticket:"):
        context.user_data["admin_action"]="set_ticket"
        await q.message.reply_text("🔢 Har ticket so‘rovida nechta ticket berilsin?\nMasalan: 2")
    elif data.startswith("give_one:"):
        context.user_data["admin_action"]="give_one_user"
        await q.message.reply_text("👤 Ticket beriladigan userning xabariga Reply qilib yuboring.\n\nYoki @username / Telegram ID yuborishingiz mumkin.")
    elif data.startswith("give_all:"):
        con=db(); amount=con.execute("SELECT ticket_amount FROM bot_groups WHERE chat_id=?",(gid,)).fetchone()[0]; cur=con.execute("UPDATE group_users SET tickets=tickets+? WHERE chat_id=?",(amount,gid)); con.commit(); con.close(); await q.message.reply_text(f"✅ {cur.rowcount} ta userga {amount} tadan ticket berildi.")
    elif data.startswith("reset_tickets:"):
        con=db(); cur=con.execute("UPDATE group_users SET tickets=0 WHERE chat_id=?",(gid,)); con.commit(); con.close(); await q.message.reply_text(f"♻️ {cur.rowcount} ta userning ticketi 0 qilindi.")
    elif data.startswith("clear_store:"):
        con=db(); cur=con.execute("DELETE FROM user_fruits WHERE group_id=?",(gid,)); con.commit(); con.close(); await q.message.reply_text(f"♻️ Shu guruh Store tozalandi. {cur.rowcount} ta yozuv o‘chirildi.")
    elif data.startswith("add_chat:"):
        context.user_data["admin_action"]="add_chat"; await q.message.reply_text("📢 Kanal/guruh ID sini yuboring.\nMasalan: -1001234567890")
    elif data.startswith("toggle_chat:"):
        context.user_data["admin_action"]="toggle_chat"; await q.message.reply_text("🔄 ON/OFF qilinadigan chatning #ID sini yuboring.\nMasalan: 1")
    elif data.startswith("remove_chat:"):
        context.user_data["admin_action"]="remove_chat"; await q.message.reply_text("➖ O‘chiriladigan chatning #ID sini yuboring.\nMasalan: 1")


async def text_action(update,context):
    action=context.user_data.get("admin_action")
    if not action: return
    gid=context.user_data.get("target_group_id")
    if not gid: return
    if not is_owner(update.effective_user.id):
        if not await is_group_admin(update,context): return
    text=(update.message.text or "").strip()
    if action=="give_one_user" and update.message.reply_to_message and update.message.reply_to_message.from_user:
        target=update.message.reply_to_message.from_user
        save_user(target)
        context.user_data["ticket_target_user_id"]=target.id
        context.user_data["admin_action"]="give_one_amount"
        keep_action=True
        await update.message.reply_text(f"👤 User topildi: {target.full_name or target.username or target.id}\n🔢 Nechta ticket berilsin?")
        return
    con=db()
    keep_action=False
    try:
        if action=="set_ticket":
            amount=int(text)
            if amount<1: raise ValueError
            con.execute("UPDATE bot_groups SET ticket_amount=? WHERE chat_id=?",(amount,gid)); con.commit(); await update.message.reply_text(f"✅ Ticket soni: {amount} ta")
        elif action=="give_one_user":
            lookup=text.lstrip("@").strip()
            if not lookup:
                raise ValueError
            if lookup.isdigit():
                row=con.execute("SELECT user_id,username,name FROM users WHERE user_id=?",(int(lookup),)).fetchone()
            else:
                row=con.execute("SELECT user_id,username,name FROM users WHERE lower(username)=lower(?)",(lookup,)).fetchone()
            if not row:
                raise LookupError
            context.user_data["ticket_target_user_id"]=row[0]
            context.user_data["admin_action"]="give_one_amount"
            keep_action=True
            await update.message.reply_text(f"👤 User topildi: {row[2] or row[1] or row[0]}\n🔢 Nechta ticket berilsin?")
        elif action=="give_one_amount":
            amount=int(text)
            if amount<1:
                raise ValueError
            target_id=context.user_data.get("ticket_target_user_id")
            if not target_id:
                raise LookupError
            con.execute("INSERT INTO group_users(chat_id,user_id,tickets) VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET tickets=tickets+excluded.tickets",(gid,target_id,amount))
            con.commit()
            await update.message.reply_text(f"✅ Userga {amount} ta ticket berildi.")
            context.user_data.pop("ticket_target_user_id",None)
        elif action=="add_chat":
            cid=int(text); chat=await context.bot.get_chat(cid)
            if chat.type not in ("channel","group","supergroup"): raise ValueError
            con.execute("INSERT INTO required_chats(group_id,chat_id,title,chat_type,enabled) VALUES(?,?,?,?,1) ON CONFLICT(group_id,chat_id) DO UPDATE SET title=excluded.title,chat_type=excluded.chat_type,enabled=1",(gid,str(chat.id),chat.title or str(chat.id),chat.type)); con.commit(); await update.message.reply_text(f"✅ {chat.title or chat.id} qo‘shildi.")
        elif action=="toggle_chat":
            rid=int(text); row=con.execute("SELECT title,enabled FROM required_chats WHERE id=? AND group_id=?",(rid,gid)).fetchone()
            if not row: raise LookupError
            new=0 if row[1] else 1; con.execute("UPDATE required_chats SET enabled=? WHERE id=?",(new,rid)); con.commit(); await update.message.reply_text(f"✅ {row[0]} — {'🟢 ON' if new else '🔴 OFF'}")
        elif action=="remove_chat":
            rid=int(text); row=con.execute("SELECT title FROM required_chats WHERE id=? AND group_id=?",(rid,gid)).fetchone()
            if not row: raise LookupError
            con.execute("DELETE FROM required_chats WHERE id=?",(rid,)); con.commit(); await update.message.reply_text(f"✅ {row[0]} o‘chirildi.")
    except ValueError:
        await update.message.reply_text("❌ Qiymat noto‘g‘ri.")
    except LookupError:
        await update.message.reply_text("❌ Bunday ID topilmadi.")
    except Exception:
        await update.message.reply_text("❌ Amal bajarilmadi. Botning chatdagi admin huquqlarini tekshiring.")
    finally:
        con.close()
        if not keep_action:
            context.user_data.pop("admin_action",None)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass


def start_health_server():
    port=int(os.getenv("PORT","10000"))
    try:
        server=HTTPServer(("0.0.0.0",port),HealthHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
    except OSError:
        pass


async def register_group(update,context):
    chat=update.effective_chat; user=update.effective_user
    if chat and chat.type in ("group","supergroup") and user and not user.is_bot:
        ensure_group(chat); save_user(user); ensure_group_user(chat.id,user.id)


def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN .env ichida topilmadi")
    init_db(); start_health_server()
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("gacha",gacha))
    app.add_handler(CommandHandler("store",store))
    app.add_handler(CommandHandler("ticket",ticket_command))
    app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CommandHandler("addchat",addchat))
    app.add_handler(CallbackQueryHandler(action_callback,pattern=r"^(set_ticket|give_one|give_all|reset_tickets|back|clear_store|add_chat|toggle_chat|remove_chat):"))
    app.add_handler(CallbackQueryHandler(panel_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_action))
    app.add_handler(MessageHandler(filters.ALL,register_group),group=1)
    print("🤖 Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
