import os
import sqlite3
import random
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = "bot.db"


def db():
    return sqlite3.connect(DB_NAME)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT,
            tickets INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_fruits (
            user_id INTEGER,
            fruit_name TEXT,
            amount INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, fruit_name)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS group_users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            tickets INTEGER DEFAULT 0,
            last_gacha INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS required_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE,
            title TEXT,
            chat_type TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            friend_id INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        INSERT OR IGNORE INTO settings (key, value)
        VALUES
            ('bot_enabled', '1'),
            ('gacha_enabled', '1'),
            ('ticket_amount', '1')
    """)

    con.commit()
    con.close()


def save_user(user):
    con = db()
    con.execute("""
        INSERT INTO users (user_id, username, name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            name=excluded.name
    """, (
        user.id,
        user.username or "",
        user.full_name or ""
    ))
    con.commit()
    con.close()


async def register_group_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    user = update.effective_user
    if not user or user.is_bot:
        return

    save_user(user)

    con = db()
    con.execute("""
        INSERT INTO group_users (chat_id, user_id, tickets)
        VALUES (?, ?, 0)
        ON CONFLICT(chat_id, user_id) DO NOTHING
    """, (update.effective_chat.id, user.id))
    con.commit()
    con.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)

    if context.args and context.args[0].startswith("ticket_"):
        try:
            request_id = int(context.args[0].split("_", 1)[1])
        except ValueError:
            request_id = 0

        if request_id:
            con = db()
            request = con.execute("""
                SELECT owner_id, status, chat_id
                FROM ticket_requests
                WHERE id=?
            """, (request_id,)).fetchone()

            if request and request[1] == "pending":
                owner_id = request[0]
                request_chat_id = request[2]
                friend_id = update.effective_user.id

                if owner_id == friend_id:
                    con.close()
                    await update.message.reply_text(
                        "❌ O‘zingizning ticket linkingizni o‘zingiz tekshira olmaysiz."
                    )
                    return

                con.execute("""
                    UPDATE ticket_requests
                    SET friend_id=?
                    WHERE id=?
                """, (friend_id, request_id))
                con.commit()

                chats = con.execute("""
                    SELECT id, title
                    FROM required_chats
                    WHERE enabled=1
                    ORDER BY id
                """).fetchall()
                con.close()

                text = (
                    "🎫 Ticket olishga yordam berish\n\n"
                    "Quyidagi kanal/guruh(lar)ga qo‘shiling:"
                )

                keyboard = []

                for chat_id, title in chats:
                    keyboard.append([
                        InlineKeyboardButton(
                            f"📢 {title}",
                            callback_data=f"ticket_chat_{chat_id}"
                        )
                    ])

                keyboard.append([
                    InlineKeyboardButton(
                        "✅ Tekshirish",
                        callback_data=f"ticket_verify_{request_id}"
                    )
                ])

                await update.message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

            con.close()

    await update.message.reply_text(
        "🍎 Blox Fruits Gacha Bot\n\n"
        "🎰 /gacha — Gacha o‘ynash\n"
        "🏪 /store — Store ko‘rish"
    )


async def gacha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)

    user_id = update.effective_user.id

    con = db()

    if update.effective_chat.type in ("group", "supergroup"):
        chat_id = update.effective_chat.id
        row = con.execute(
            "SELECT tickets FROM group_users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
    else:
        chat_id = None
        row = con.execute(
            "SELECT tickets FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    if not row or row[0] <= 0:
        con.close()

        keyboard = [[
            InlineKeyboardButton(
                "🎫 Ticket olish",
                callback_data="get_ticket"
            )
        ]]

        await update.message.reply_text(
            "❌ Sizda ticket yo‘q!\n\n"
            "🎰 Gacha o‘ynash uchun ticket oling.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    fruits = con.execute("""
        SELECT name, chance
        FROM fruits
        WHERE enabled=1 AND chance>0
    """).fetchall()

    if not fruits:
        con.close()
        await update.message.reply_text(
            "❌ Hozircha aktiv fruitlar mavjud emas."
        )
        return

    names = [row[0] for row in fruits]
    chances = [row[1] for row in fruits]
    fruit_name = random.choices(names, weights=chances, k=1)[0]
    chance = next(row[1] for row in fruits if row[0] == fruit_name)

    if chat_id is not None:
        con.execute(
            "UPDATE group_users SET tickets=tickets-1, last_gacha=? WHERE chat_id=? AND user_id=?",
            (int(__import__("time").time()), chat_id, user_id)
        )
    else:
        con.execute(
            "UPDATE users SET tickets=tickets-1, last_gacha=? WHERE user_id=?",
            (int(__import__("time").time()), user_id)
        )

    con.execute("""
        INSERT INTO user_fruits (user_id, fruit_name, amount)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, fruit_name)
        DO UPDATE SET amount=amount+1
    """, (user_id, fruit_name))

    con.commit()
    con.close()

    if fruit_name == "Dragon":
        image_path = "fruit_images/Dragon_East.png"
    else:
        image_path = f"fruit_images/{fruit_name}_Fruit.png"

    caption = (
        f"🍎 {fruit_name}\n"
        f"🎲 Tushish shansi: {chance:.10f}%"
    )

    if os.path.exists(image_path):
        with open(image_path, "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=caption
            )
    else:
        await update.message.reply_text(caption)


async def store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)

    user_id = update.effective_user.id

    if context.args:
        username = context.args[0].lstrip("@")

        con = db()
        row = con.execute(
            "SELECT user_id, name, username FROM users "
            "WHERE lower(username)=lower(?)",
            (username,)
        ).fetchone()
        con.close()

        if not row:
            await update.message.reply_text(
                "❌ Bu foydalanuvchi topilmadi."
            )
            return

        user_id = row[0]
        title = f"🏪 @{row[2]}" if row[2] else f"🏪 {row[1]}"
    else:
        title = "🏪 Sizning Store"

    con = db()
    fruits = con.execute("""
        SELECT fruit_name, amount
        FROM user_fruits
        WHERE user_id=? AND amount>0
        ORDER BY fruit_name
    """, (user_id,)).fetchall()
    con.close()

    if not fruits:
        await update.message.reply_text(
            f"{title}\n\n"
            "📦 Store hozircha bo‘sh."
        )
        return

    text = f"{title}\n\n"
    for fruit_name, amount in fruits:
        text += f"🍎 {fruit_name} × {amount}\n"

    await update.message.reply_text(text)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Siz admin emassiz.")
        return

    keyboard = [
        [InlineKeyboardButton("🎫 Ticket", callback_data="admin_ticket")],
        [InlineKeyboardButton("📢 Kanal/Guruh", callback_data="admin_chats")],
        [InlineKeyboardButton("🏪 Store", callback_data="admin_store")],
        [InlineKeyboardButton("🎰 Gacha ON/OFF", callback_data="admin_gacha")],
        [InlineKeyboardButton("🤖 Bot ON/OFF", callback_data="admin_bot")],
    ]

    await update.message.reply_text(
        "⚙️ ADMIN PANEL",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )



async def admin_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    con = db()
    chats = con.execute("""
        SELECT id, chat_id, title, chat_type, enabled
        FROM required_chats
        ORDER BY id
    """).fetchall()
    con.close()

    text = "📢 KANAL/GURUH SOZLAMALARI\n\n"

    if chats:
        for row_id, chat_id, title, chat_type, enabled in chats:
            status = "🟢 Yoqilgan" if enabled else "🔴 O‘chirilgan"
            text += (
                f"#{row_id} — {title}\n"
                f"ID: {chat_id}\n"
                f"Turi: {chat_type}\n"
                f"Holati: {status}\n\n"
            )
    else:
        text += "📭 Hozircha kanal/guruh qo‘shilmagan.\n"

    keyboard = [
        [InlineKeyboardButton("➕ Kanal/Guruh qo‘shish", callback_data="admin_chat_add")],
        [InlineKeyboardButton("➖ Kanal/Guruh o‘chirish", callback_data="admin_chat_remove")],
        [InlineKeyboardButton("🔄 ON/OFF", callback_data="admin_chat_toggle")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back")],
    ]

    await update.callback_query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "admin_ticket":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_ticket_chat_id"] = query.message.chat_id

        con = db()
        row = con.execute(
            "SELECT value FROM settings WHERE key='ticket_amount'"
        ).fetchone()
        con.close()

        ticket_amount = int(row[0]) if row else 1

        keyboard = [
            [InlineKeyboardButton(
                f"🎫 Beriladigan ticket: {ticket_amount}",
                callback_data="admin_ticket_amount"
            )],
            [InlineKeyboardButton(
                "👥 Hammaga ticket berish",
                callback_data="admin_ticket_all"
            )],
            [InlineKeyboardButton(
                "👤 Bitta userga ticket",
                callback_data="admin_ticket_user"
            )],
            [InlineKeyboardButton(
                "♻️ Hammaga ticket = 0",
                callback_data="admin_ticket_reset_all"
            )],
            [InlineKeyboardButton(
                "👤 Bitta user ticket = 0",
                callback_data="admin_ticket_reset_user"
            )],
            [InlineKeyboardButton(
                "🔙 Orqaga",
                callback_data="admin_back"
            )],
        ]

        await query.message.reply_text(
            "🎫 TICKET SOZLAMALARI\n\n"
            f"Har berishda: {ticket_amount} ta ticket",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data == "admin_ticket_all":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        con = db()
        row = con.execute(
            "SELECT value FROM settings WHERE key='ticket_amount'"
        ).fetchone()

        amount = int(row[0]) if row else 1

        ticket_chat_id = context.user_data.get("admin_ticket_chat_id")

        if ticket_chat_id is not None and ticket_chat_id < 0:
            con.execute("""
                UPDATE group_users
                SET tickets = COALESCE(tickets, 0) + ?
                WHERE chat_id=?
            """, (amount, ticket_chat_id))

            count = con.execute(
                "SELECT COUNT(*) FROM group_users WHERE chat_id=?",
                (ticket_chat_id,)
            ).fetchone()[0]
        else:
            con.execute("""
                UPDATE users
                SET tickets = COALESCE(tickets, 0) + ?
            """, (amount,))

            count = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

        con.commit()
        con.close()

        await query.message.reply_text(
            f"✅ Barcha {count} ta userga {amount} tadan ticket berildi."
        )
        return

    if query.data == "admin_ticket_reset_all":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        ticket_chat_id = context.user_data.get("admin_ticket_chat_id")

        if ticket_chat_id is not None and ticket_chat_id < 0:
            cur = con.execute("""
                UPDATE group_users
                SET tickets=0
                WHERE chat_id=?
            """, (ticket_chat_id,))
        else:
            cur = con.execute("""
                UPDATE users
                SET tickets=0
            """)
        count = cur.rowcount
        con.commit()
        con.close()

        await query.message.reply_text(
            f"♻️ Barcha userlarning ticketlari 0 qilindi.\n"
            f"👥 Userlar: {count}"
        )
        return

    if query.data == "admin_ticket_reset_user":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "ticket_reset_user"

        await query.message.reply_text(
            "👤 Ticketi 0 qilinadigan userni yuboring.\n\n"
            "Masalan:\n"
            "@username\n"
            "yoki\n"
            "123456789"
        )
        return

    if query.data == "admin_ticket_user":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "ticket_user"

        await query.message.reply_text(
            "👤 Ticket beriladigan userni yuboring.\n\n"
            "Masalan:\n"
            "@username\n"
            "yoki\n"
            "123456789"
        )
        return

    if query.data == "admin_ticket_amount":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "ticket_amount"

        await query.message.reply_text(
            "🔢 Nechta ticket berilishini kiriting.\n\n"
            "Masalan: 5\n"
            "❌ Bekor qilish uchun /cancel"
        )
        return

    if query.data == "admin_bot":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        con = db()
        row = con.execute(
            "SELECT value FROM settings WHERE key='bot_enabled'"
        ).fetchone()

        current = int(row[0]) if row else 1
        new_status = 0 if current else 1

        con.execute("""
            INSERT INTO settings (key, value)
            VALUES ('bot_enabled', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (str(new_status),))

        con.commit()
        con.close()

        status_text = "🟢 Bot YOQILDI" if new_status else "🔴 Bot O‘CHIRILDI"

        await query.message.reply_text(
            f"🤖 Bot holati: {status_text}"
        )
        return

    if query.data == "admin_gacha":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        con = db()
        row = con.execute(
            "SELECT value FROM settings WHERE key='gacha_enabled'"
        ).fetchone()

        current = int(row[0]) if row else 1
        new_status = 0 if current else 1

        con.execute("""
            INSERT INTO settings (key, value)
            VALUES ('gacha_enabled', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (str(new_status),))

        con.commit()
        con.close()

        status_text = "🟢 Gacha YOQILDI" if new_status else "🔴 Gacha O‘CHIRILDI"

        await query.message.reply_text(
            f"🎰 Gacha holati: {status_text}"
        )
        return

    if query.data == "admin_store":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        keyboard = [
            [InlineKeyboardButton(
                "♻️ Hammaga Store = 0",
                callback_data="admin_store_reset_all"
            )],
            [InlineKeyboardButton(
                "👤 Bitta user Store = 0",
                callback_data="admin_store_reset_user"
            )],
            [InlineKeyboardButton(
                "🔙 Orqaga",
                callback_data="admin_back"
            )],
        ]

        await query.message.reply_text(
            "🏪 STORE SOZLAMALARI",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data == "admin_store_reset_all":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        con = db()
        cur = con.execute("DELETE FROM user_fruits")
        count = cur.rowcount
        con.commit()
        con.close()

        await query.message.reply_text(
            f"♻️ Barcha userlarning Store'i 0 qilindi.\n"
            f"🧹 O‘chirilgan yozuvlar: {count}"
        )
        return

    if query.data == "admin_store_reset_user":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "store_reset_user"

        await query.message.reply_text(
            "👤 Store'i 0 qilinadigan userni yuboring.\n\n"
            "Masalan:\n"
            "@username\n"
            "yoki\n"
            "123456789"
        )
        return

    if query.data == "admin_chat_toggle":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "chat_toggle"

        await query.message.reply_text(
            "🔄 ON/OFF qilinadigan kanal/guruhning ID raqamini yuboring.\n\n"
            "Masalan: 1"
        )
        return

    if query.data == "admin_chat_remove":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "chat_remove"

        await query.message.reply_text(
            "➖ O‘chiriladigan kanal/guruhning ID raqamini yuboring.\n\n"
            "Masalan: 1"
        )
        return

    if query.data == "admin_chat_add":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        context.user_data["admin_action"] = "chat_add"

        await query.message.reply_text(
            "📢 Kanal yoki guruhni qo‘shish\n\n"
            "Bot admin bo‘lgan kanal/guruh ID sini yuboring.\n"
            "Masalan: -1001234567890"
        )
        return

    if query.data == "admin_chats":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        await admin_chats(update, context)
        return

    if query.data == "admin_back":
        if query.from_user.id != OWNER_ID:
            await query.message.reply_text("❌ Siz admin emassiz.")
            return

        keyboard = [
            [InlineKeyboardButton("🎫 Ticket", callback_data="admin_ticket")],
            [InlineKeyboardButton("📢 Kanal/Guruh", callback_data="admin_chats")],
            [InlineKeyboardButton("🏪 Store", callback_data="admin_store")],
            [InlineKeyboardButton("🎰 Gacha ON/OFF", callback_data="admin_gacha")],
            [InlineKeyboardButton("🤖 Bot ON/OFF", callback_data="admin_bot")],
        ]

        await query.message.reply_text(
            "⚙️ ADMIN PANEL",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data == "get_ticket":
        owner_id = query.from_user.id

        con = db()
        cur = con.cursor()

        request_chat_id = query.message.chat_id if query.message else None

        cur.execute("""
            INSERT INTO ticket_requests (owner_id, status, created_at, chat_id)
            VALUES (?, 'pending', ?, ?)
        """, (owner_id, int(time.time()), request_chat_id))

        request_id = cur.lastrowid
        con.commit()
        con.close()

        me = await context.bot.get_me()
        invite_link = f"https://t.me/{me.username}?start=ticket_{request_id}"

        share_url = (
            "https://t.me/share/url"
            f"?url={invite_link}"
            "&text=🎫 Menga ticket olishga yordam ber!"
        )

        keyboard = [[
            InlineKeyboardButton(
                "📤 Do‘stga yuborish",
                url=share_url
            )
        ]]

        await query.message.reply_text(
            "🎫 Ticket olish uchun quyidagi linkni do‘stingizga yuboring:\n\n"
            f"{invite_link}\n\n"
            "👤 Do‘stingiz linkni ochib, kerakli kanal/guruhlarga "
            "qo‘shilib, tekshiruvdan o‘tishi kerak.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data.startswith("ticket_chat_"):
        try:
            chat_row_id = int(query.data.split("_")[-1])
        except ValueError:
            await query.message.reply_text("❌ Chat ma’lumotini aniqlab bo‘lmadi.")
            return

        con = db()
        row = con.execute("""
            SELECT chat_id, title
            FROM required_chats
            WHERE id=? AND enabled=1
        """, (chat_row_id,)).fetchone()
        con.close()

        if not row:
            await query.message.reply_text("❌ Bu chat hozir faol emas.")
            return

        chat_id, title = row

        try:
            chat = await context.bot.get_chat(chat_id)

            if chat.username:
                url = f"https://t.me/{chat.username}"
            else:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=chat_id
                )
                url = invite.invite_link

            await query.message.reply_text(
                f"📢 {title} ga qo‘shilish uchun tugmani bosing:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📢 Qo‘shilish", url=url)
                ]])
            )
        except Exception:
            await query.message.reply_text(
                f"📢 {title}\n\n"
                "❌ Bu chat uchun qo‘shilish havolasini olishda xatolik yuz berdi."
            )
        return

    if query.data.startswith("ticket_verify_"):
        try:
            request_id = int(query.data.split("_")[-1])
        except ValueError:
            await query.message.reply_text("❌ Request ma’lumotini aniqlab bo‘lmadi.")
            return

        friend_id = query.from_user.id

        con = db()
        request = con.execute("""
            SELECT owner_id, friend_id, status, chat_id
            FROM ticket_requests
            WHERE id=?
        """, (request_id,)).fetchone()

        if not request:
            con.close()
            await query.message.reply_text("❌ Ticket so‘rovi topilmadi.")
            return

        owner_id, saved_friend_id, status, request_chat_id = request

        if status != "pending":
            con.close()
            await query.message.reply_text(
                "ℹ️ Bu ticket so‘rovi allaqachon ishlatilgan."
            )
            return

        if saved_friend_id and saved_friend_id != friend_id:
            con.close()
            await query.message.reply_text(
                "❌ Bu link boshqa foydalanuvchi uchun ochilgan."
            )
            return

        chats = con.execute("""
            SELECT id, chat_id, title
            FROM required_chats
            WHERE enabled=1
            ORDER BY id
        """).fetchall()
        con.close()

        missing = []

        for chat_row_id, chat_id, title in chats:
            try:
                member = await context.bot.get_chat_member(
                    chat_id,
                    friend_id
                )

                joined = member.status in ("member", "administrator", "creator")

                if member.status == "restricted":
                    joined = getattr(member, "is_member", False)

                if not joined:
                    missing.append(title)

            except Exception:
                missing.append(title)

        if missing:
            text = "❌ Hali barcha kanal/guruhlarga qo‘shilmagansiz.\n\n"
            text += "Qo‘shilmaganlar:\n"
            text += "\n".join(f"• {title}" for title in missing)
            text += "\n\nQo‘shilgandan keyin yana `✅ Tekshirish` tugmasini bosing."

            await query.message.reply_text(text)
            return

        con = db()

        request = con.execute("""
            SELECT owner_id, friend_id, status, chat_id
            FROM ticket_requests
            WHERE id=?
        """, (request_id,)).fetchone()

        if not request or request[2] != "pending":
            con.close()
            await query.message.reply_text(
                "ℹ️ Bu ticket so‘rovi allaqachon ishlatilgan."
            )
            return

        owner_id = request[0]
        request_chat_id = request[3]

        if request_chat_id is not None and request_chat_id < 0:
            con.execute("""
                INSERT INTO group_users (chat_id, user_id, tickets)
                VALUES (?, ?, 1)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET tickets = COALESCE(tickets, 0) + 1
            """, (request_chat_id, owner_id))
        else:
            con.execute("""
                UPDATE users
                SET tickets = COALESCE(tickets, 0) + 1
                WHERE user_id=?
            """, (owner_id,))

        con.execute("""
            UPDATE ticket_requests
            SET friend_id=?, status='completed'
            WHERE id=? AND status='pending'
        """, (friend_id, request_id))

        con.commit()
        con.close()

        await query.message.reply_text(
            "✅ Tekshiruv muvaffaqiyatli!\n\n"
            "🎫 Ticket link egasiga berildi."
        )

        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text="🎉 Do‘stingiz tekshiruvdan o‘tdi!\n\n"
                     "🎫 Sizga 1 ta ticket berildi."
            )
        except Exception:
            pass
        return


async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    action = context.user_data.get("admin_action")

    if action == "chat_toggle":
        text = (update.message.text or "").strip()

        try:
            row_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ ID noto‘g‘ri. Masalan: 1"
            )
            return

        con = db()
        chat = con.execute(
            "SELECT title, enabled FROM required_chats WHERE id=?",
            (row_id,)
        ).fetchone()

        if not chat:
            con.close()
            await update.message.reply_text(
                "❌ Bunday kanal/guruh ID si topilmadi."
            )
            return

        new_status = 0 if chat[1] else 1

        con.execute(
            "UPDATE required_chats SET enabled=? WHERE id=?",
            (new_status, row_id)
        )
        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)

        status_text = "🟢 Yoqildi" if new_status else "🔴 O‘chirildi"

        await update.message.reply_text(
            f"✅ {chat[0]} — {status_text}"
        )
        return

    if action == "chat_remove":
        text = (update.message.text or "").strip()

        try:
            row_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ ID noto‘g‘ri. Masalan: 1"
            )
            return

        con = db()
        chat = con.execute(
            "SELECT title FROM required_chats WHERE id=?",
            (row_id,)
        ).fetchone()

        if not chat:
            con.close()
            await update.message.reply_text(
                "❌ Bunday kanal/guruh ID si topilmadi."
            )
            return

        con.execute(
            "DELETE FROM required_chats WHERE id=?",
            (row_id,)
        )
        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)

        await update.message.reply_text(
            f"✅ {chat[0]} kanal/guruhi o‘chirildi."
        )
        return

    if action == "chat_add":
        text = (update.message.text or "").strip()

        try:
            chat_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Chat ID noto‘g‘ri. Masalan: -1001234567890"
            )
            return

        try:
            chat = await context.bot.get_chat(chat_id)
        except Exception:
            await update.message.reply_text(
                "❌ Kanal/guruh topilmadi yoki bot u yerga kira olmaydi."
            )
            return

        if chat.type not in ("channel", "group", "supergroup"):
            await update.message.reply_text(
                "❌ Faqat kanal yoki guruh qo‘shish mumkin."
            )
            return

        con = db()

        con.execute("""
            INSERT INTO required_chats (chat_id, title, chat_type, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                chat_type=excluded.chat_type,
                enabled=1
        """, (
            str(chat.id),
            chat.title or str(chat.id),
            chat.type
        ))

        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)

        await update.message.reply_text(
            f"✅ Kanal/guruh qo‘shildi!\n\n"
            f"📢 {chat.title or chat.id}\n"
            f"🆔 {chat.id}\n"
            f"📌 Turi: {chat.type}"
        )
        return

    if action == "store_reset_user":
        text = (update.message.text or "").strip()
        target = text.lstrip("@").strip()

        con = db()

        if target.isdigit():
            user = con.execute(
                "SELECT user_id, username, name FROM users WHERE user_id=?",
                (int(target),)
            ).fetchone()
        else:
            user = con.execute(
                "SELECT user_id, username, name FROM users WHERE LOWER(username)=LOWER(?)",
                (target,)
            ).fetchone()

        if not user:
            con.close()
            await update.message.reply_text(
                "❌ User topilmadi."
            )
            return

        cur = con.execute(
            "DELETE FROM user_fruits WHERE user_id=?",
            (user[0],)
        )
        deleted = cur.rowcount

        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)

        username_text = f"@{user[1]}" if user[1] else str(user[0])

        await update.message.reply_text(
            f"♻️ {username_text} ning Store'i 0 qilindi.\n"
            f"🍎 O‘chirilgan fruit yozuvlari: {deleted}"
        )
        return

    if action == "ticket_reset_user":
        text = (update.message.text or "").strip()
        target = text.lstrip("@").strip()

        con = db()

        if target.isdigit():
            user = con.execute(
                "SELECT user_id, username, name FROM users WHERE user_id=?",
                (int(target),)
            ).fetchone()
        else:
            user = con.execute(
                "SELECT user_id, username, name FROM users WHERE LOWER(username)=LOWER(?)",
                (target,)
            ).fetchone()

        if not user:
            con.close()
            await update.message.reply_text(
                "❌ User topilmadi."
            )
            return

        ticket_chat_id = context.user_data.get("admin_ticket_chat_id")

        if ticket_chat_id is not None and ticket_chat_id < 0:
            con.execute(
                "UPDATE group_users SET tickets=0 WHERE chat_id=? AND user_id=?",
                (ticket_chat_id, user[0])
            )
        else:
            con.execute(
                "UPDATE users SET tickets=0 WHERE user_id=?",
                (user[0],)
            )

        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)

        username_text = f"@{user[1]}" if user[1] else str(user[0])

        await update.message.reply_text(
            f"♻️ {username_text} ning ticketlari 0 qilindi."
        )
        return

    if action == "ticket_user":
        text = (update.message.text or "").strip()
        target = text.lstrip("@").strip()

        con = db()

        if target.isdigit():
            user = con.execute(
                "SELECT user_id, username, name FROM users WHERE user_id=?",
                (int(target),)
            ).fetchone()
        else:
            user = con.execute(
                "SELECT user_id, username, name FROM users WHERE LOWER(username)=LOWER(?)",
                (target,)
            ).fetchone()

        if not user:
            con.close()
            await update.message.reply_text(
                "❌ User topilmadi. U botga kamida bir marta /start bosgan bo‘lishi kerak."
            )
            return

        row = con.execute(
            "SELECT value FROM settings WHERE key='ticket_amount'"
        ).fetchone()
        amount = int(row[0]) if row else 1

        ticket_chat_id = context.user_data.get("admin_ticket_chat_id")

        if ticket_chat_id is not None and ticket_chat_id < 0:
            con.execute("""
                INSERT INTO group_users (chat_id, user_id, tickets)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET tickets = COALESCE(group_users.tickets, 0) + excluded.tickets
            """, (ticket_chat_id, user[0], amount))
        else:
            con.execute("""
                UPDATE users
                SET tickets = COALESCE(tickets, 0) + ?
                WHERE user_id=?
            """, (amount, user[0]))

        con.commit()
        con.close()

        context.user_data.pop("admin_action", None)

        username_text = f"@{user[1]}" if user[1] else str(user[0])

        await update.message.reply_text(
            f"✅ {username_text} ga {amount} ta ticket berildi."
        )

        try:
            await context.bot.send_message(
                chat_id=user[0],
                text=f"🎫 Admin sizga {amount} ta ticket berdi!"
            )
        except Exception:
            pass

        return

    if action != "ticket_amount":
        return

    text = (update.message.text or "").strip()

    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text("❌ Faqat butun son kiriting. Masalan: 5")
        return

    if amount < 1:
        await update.message.reply_text("❌ Ticket soni 1 yoki undan katta bo‘lishi kerak.")
        return

    con = db()
    con.execute("""
        INSERT INTO settings (key, value)
        VALUES ('ticket_amount', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (str(amount),))
    con.commit()
    con.close()

    context.user_data.pop("admin_action", None)

    await update.message.reply_text(
        f"✅ Endi beriladigan ticket soni: {amount} ta"
    )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN .env ichida topilmadi")

    init_db()
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gacha", gacha))
    app.add_handler(CommandHandler("store", store))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message_handler))
    app.add_handler(MessageHandler(filters.ALL, register_group_user), group=1)

    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
