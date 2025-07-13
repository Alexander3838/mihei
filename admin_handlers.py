import sqlite3
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def handle_check_screenshots(update, context):
    user_id = update.effective_user.id
    from config import ADMIN_ID
    if user_id != ADMIN_ID:
        update.message.reply_text("⛔ У вас нет прав на эту команду.")
        return

    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, file_id FROM screenshots ORDER BY timestamp ASC LIMIT 1")
    row = cur.fetchone()
    conn.close()

    if not row:
        update.message.reply_text("✅ Нет скриншотов для проверки.")
        return

    screenshot_id, user_who_sent, file_id = row

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{screenshot_id}"),
            InlineKeyboardButton("❌ Удалить", callback_data=f"delete_{screenshot_id}"),
            InlineKeyboardButton("🚫 Забанить", callback_data=f"ban_{user_who_sent}")
        ],
        [
            InlineKeyboardButton("➡️ Пропустить", callback_data="skip")
        ]
    ])

    context.bot.send_photo(
        chat_id=user_id,
        photo=file_id,
        caption=f"👤 Пользователь: {user_who_sent}\n🆔 Скриншот #{screenshot_id}",
        reply_markup=keyboard
    )

def delete_screen_command(update, context):
    update.message.reply_text("🗑 Используйте кнопки под скриншотом для удаления.")

def cleanup_old_screenshots():
    import time
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    threshold = time.time() - 86400  # 24 часа
    cur.execute("DELETE FROM screenshots WHERE timestamp < ?", (threshold,))
    conn.commit()
    conn.close()