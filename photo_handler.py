import sqlite3
import time

def handle_photo(update, context):
    user_id = update.effective_user.id

    photo_sizes = update.message.photo
    if not photo_sizes:
        update.message.reply_text("❌ Не удалось получить фото, попробуйте ещё раз.")
        return

    file_id = photo_sizes[-1].file_id  # берём фото с максимальным разрешением

    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()

    cur.execute("INSERT INTO screenshots (user_id, file_id, timestamp) VALUES (?, ?, ?)", 
                (user_id, file_id, time.time()))

    conn.commit()
    conn.close()

    update.message.reply_text("✅ Скриншот получен! Мы проверим его в ближайшее время.")