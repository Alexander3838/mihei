import time
import sqlite3
import textwrap
import threading
import traceback
import urllib.parse  # ✅ добавляем для кодирования ссылок
from flask import Flask, request, redirect
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
from config import TOKEN, ADMIN_ID
from keep_alive import keep_alive
from photo_handler import handle_photo
from admin_handlers import handle_check_screenshots, delete_screen_command, cleanup_old_screenshots

# ✅ Добавляем глобальную блокировку БД
db_lock = threading.Lock()

# Flask-сервер для отслеживания переходов по ссылке
web_app = Flask('')

@web_app.route('/click')
def track_click():
    user_id = request.args.get('user_id')
    video_link = request.args.get('video_link')

    if user_id and video_link:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS click_log (
                user_id INTEGER,
                video_link TEXT,
                timestamp REAL,
                PRIMARY KEY (user_id, video_link)
            )
        """)
        # Добавляем текущий timestamp при клике
        cur.execute("INSERT OR IGNORE INTO click_log (user_id, video_link, timestamp) VALUES (?, ?, ?)", (user_id, video_link, time.time()))
        conn.commit()
        conn.close()

        # Перенаправляем пользователя на оригинальное видео
        return redirect(video_link)

    return "❌ Ошибка: нет данных"

@web_app.route('/')
def home():
    return "✅ Я жив!"

def run_web():
    web_app.run(host='0.0.0.0', port=8080)

def keep_alive():
    threading.Thread(target=run_web, daemon=True).start()

# Отправка длинного сообщения частями
def send_long_message(update, text, max_len=4000):
    parts = textwrap.wrap(text, width=max_len, break_long_words=False, break_on_hyphens=False)
    for part in parts:
        update.message.reply_text(part)

def init_db():
    print("✅ init_db() ЗАПУЩЕНА")
    with db_lock:
        with sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10) as conn:
            cur = conn.cursor()

            # Таблица пользователей
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    likes_given INTEGER DEFAULT 0,
                    likes_received INTEGER DEFAULT 0,
                    invited_by INTEGER,
                    banned INTEGER DEFAULT 0,
                    warnings INTEGER DEFAULT 0,
                    rating INTEGER DEFAULT 0
                )
            """)

            # Таблица видео
            cur.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    link TEXT UNIQUE,
                    timestamp REAL
                )
            """)

            # Таблица заданий
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    user_id INTEGER PRIMARY KEY,
                    links TEXT,
                    task_time REAL,
                    task_done INTEGER DEFAULT 0
                )
            """)

            # Таблица лайков
            cur.execute("""
                CREATE TABLE IF NOT EXISTS likes_log (
                    user_id INTEGER,
                    video_link TEXT,
                    PRIMARY KEY (user_id, video_link)
                )
            """)

            # Таблица уведомлений
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notify_log (
                    user_id INTEGER PRIMARY KEY,
                    last_notify REAL
                )
            """)

            # Таблица кликов
            cur.execute("""
                CREATE TABLE IF NOT EXISTS click_log (
                    user_id INTEGER,
                    video_link TEXT,
                    timestamp REAL,
                    PRIMARY KEY (user_id, video_link)
                )
            """)

            # Таблица скриншотов
            cur.execute("""
                CREATE TABLE IF NOT EXISTS screenshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    file_id TEXT,
                    timestamp REAL
                )
            """)

            conn.commit()

def handle_screenshot_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != ADMIN_ID:
        query.answer("⛔ У вас нет прав для этого действия.")
        return

    data = query.data
    with db_lock:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()

        if data.startswith("confirm_"):
            screenshot_id = int(data.split("_")[1])

            # Получаем user_id, чей скриншот
            cur.execute("SELECT user_id FROM screenshots WHERE id=?", (screenshot_id,))
            s_row = cur.fetchone()
            if s_row:
                confirmed_user_id = s_row[0]
                # Повышаем рейтинг
                cur.execute("UPDATE users SET rating = rating + 1 WHERE user_id=?", (confirmed_user_id,))

                # Отправляем уведомление пользователю
                try:
                    context.bot.send_message(
                        chat_id=confirmed_user_id,
                        text=(
                            "✅ Ваш скриншот лайка принят!\n"
                            "Теперь вы можете выполнять задания без скриншотов, если честно ставите лайки. "
                            "Если будет подозрение на обман — скриншоты снова станут обязательными."
                        )
                    )
                except Exception as e:
                    print(f"[!] Ошибка при отправке уведомления пользователю {confirmed_user_id}: {e}")

            # Удаляем скрин
            cur.execute("DELETE FROM screenshots WHERE id=?", (screenshot_id,))
            query.answer("✅ Скрин подтверждён и удалён из очереди.")
            handle_check_screenshots(update, context)

        elif data.startswith("delete_"):
            screenshot_id = int(data.split("_")[1])
            cur.execute("DELETE FROM screenshots WHERE id=?", (screenshot_id,))
            query.edit_message_caption(caption="❌ Скриншот удалён из очереди.")
            query.answer("Скриншот удалён.")

        elif data.startswith("ban_"):
            ban_user_id = int(data.split("_")[1])
            cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (ban_user_id,))
            cur.execute("DELETE FROM screenshots WHERE user_id=?", (ban_user_id,))
            query.edit_message_caption(caption=f"🚫 Пользователь {ban_user_id} заблокирован. Скриншоты удалены.")
            query.answer("Пользователь заблокирован.")

        conn.commit()
        conn.close()

def handle_callback_query(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if user_id != ADMIN_ID:
        query.answer("⛔ У вас нет прав на это действие.", show_alert=True)
        return

    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()

    if data.startswith("confirm_"):
        screenshot_id = int(data.split("_")[1])

        # Получаем user_id, чей скриншот
        cur.execute("SELECT user_id FROM screenshots WHERE id=?", (screenshot_id,))
        s_row = cur.fetchone()
        if s_row:
            confirmed_user_id = s_row[0]
            # Повышаем рейтинг
            cur.execute("UPDATE users SET rating = rating + 1 WHERE user_id=?", (confirmed_user_id,))

            # (опционально) отправляем уведомление пользователю
            try:
                context.bot.send_message(
                    chat_id=confirmed_user_id,
                    text="✅ Ваш скриншот лайка принят! Теперь вы можете подтверждать задания."
                )
            except:
                pass  # пользователь мог заблокировать бота

        # Удаляем скрин
        cur.execute("DELETE FROM screenshots WHERE id=?", (screenshot_id,))
        query.answer("✅ Скрин подтвержден и удалён из очереди.")
        handle_check_screenshots(update, context)

    elif data.startswith("delete_"):
        screenshot_id = int(data.split("_")[1])
        cur.execute("DELETE FROM screenshots WHERE id=?", (screenshot_id,))
        query.answer("❌ Скрин удалён.")
        handle_check_screenshots(update, context)

    elif data.startswith("ban_"):
        banned_user_id = int(data.split("_")[1])
        # Баним пользователя
        cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (banned_user_id,))
        # Удаляем все скриншоты этого пользователя
        cur.execute("DELETE FROM screenshots WHERE user_id=?", (banned_user_id,))
        query.answer(f"🚫 Пользователь {banned_user_id} забанен и его скрины удалены.")
        handle_check_screenshots(update, context)

    elif data.startswith("skip_"):
        query.answer("⏭ Пропущено.")
        handle_check_screenshots(update, context)

    else:
        query.answer("❓ Неизвестная команда.")

    conn.commit()
    conn.close()        

def handle_check_screenshots(update, context):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        update.effective_message.reply_text("⛔ У вас нет прав на эту команду.")
        return

    with db_lock:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()

        # Получаем все скриншоты
        cur.execute("SELECT id, user_id, file_id FROM screenshots ORDER BY timestamp ASC")
        rows = cur.fetchall()

        if not rows:
            conn.close()
            update.effective_message.reply_text("✅ Нет скриншотов для проверки.")
            return

        screenshot_id, user_who_sent, file_id = rows[0]
        total_left = len(rows) - 1
        conn.close()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{screenshot_id}"),
            InlineKeyboardButton("❌ Удалить", callback_data=f"delete_{screenshot_id}"),
            InlineKeyboardButton("🚫 Забанить", callback_data=f"ban_{user_who_sent}")
        ],
        [
            InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip_{screenshot_id}")
        ]
    ])

    update.effective_message.reply_photo(
        photo=file_id,
        caption=(
            f"👤 Пользователь: {user_who_sent}\n"
            f"🖼 Осталось на проверку: {total_left}"
        ),
        reply_markup=keyboard
    )

def cleanup_old_videos():
    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()
    cutoff = time.time() - 86400  # 24 часа назад

    # Удаляем видео старше 24 часов, которые не получили лайков
    cur.execute("""
        DELETE FROM videos
        WHERE timestamp < ?
        AND link NOT IN (SELECT video_link FROM likes_log)
    """, (cutoff,))

    conn.commit()
    conn.close()

def is_tiktok_link(text):
    return "tiktok.com" in text.lower()

def register_user(user_id, invited_by=None):
    with db_lock:
        with sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10) as conn:
            cur = conn.cursor()
            if invited_by and invited_by != user_id:
                cur.execute("INSERT OR IGNORE INTO users (user_id, invited_by) VALUES (?, ?)", (user_id, invited_by))
            else:
                cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            conn.commit()

def add_video(user_id, link, is_admin=False):
    with db_lock:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()

        # Проверяем, есть ли уже такое видео у пользователя
        cur.execute("SELECT link FROM videos WHERE user_id=? AND link=?", (user_id, link))
        if cur.fetchone():
            conn.close()
            return "⚠️ Это видео уже добавлено."

        # Ограничение для обычных пользователей: не более 1 видео
        if not is_admin:
            cur.execute("SELECT COUNT(*) FROM videos WHERE user_id=?", (user_id,))
            count = cur.fetchone()[0]
            if count >= 1:
                conn.close()
                return "❗ Вы можете добавить только 1 видео. Чтобы оно попало в очередь, выполните задания!"

        # Добавляем видео с текущим временем
        cur.execute("INSERT INTO videos (user_id, link, timestamp) VALUES (?, ?, ?)", (user_id, link, time.time()))

        conn.commit()
        conn.close()

        return "✅ Видео добавлено! Чтобы оно попало в очередь, лайкните 3 видео."

def get_tasks(user_id):
    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()

    # Проверяем активное задание
    cur.execute("SELECT links, task_done FROM tasks WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        links, task_done = row
        conn.close()
        return links.split(","), task_done

    # Получаем видео, которые пользователь лайкал
    cur.execute("SELECT video_link FROM likes_log WHERE user_id=?", (user_id,))
    liked_links = set(row[0] for row in cur.fetchall())

    # Получаем ссылки видео других пользователей, которые не лайкал
    cur.execute("SELECT link FROM videos WHERE user_id != ?", (user_id,))
    all_links = [row[0] for row in cur.fetchall()]
    unique_links = [link for link in all_links if link not in liked_links]

    # Берём до 3 новых заданий
    links = unique_links[:3]

    if links:
        now = time.time()
        cur.execute("REPLACE INTO tasks (user_id, links, task_time, task_done) VALUES (?, ?, ?, 0)", (user_id, ",".join(links), now))
        conn.commit()

    conn.close()
    return links, 0

def confirm_likes(user_id):
    print(f"confirm_likes вызвана для пользователя {user_id}")
    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()

    # Проверяем бан, предупреждения и рейтинг
    cur.execute("SELECT banned, warnings, rating FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        banned, warnings, rating = row
        if banned == 1:
            conn.close()
            return "⛔ Вы заблокированы за нечестное выполнение заданий."
    else:
        conn.close()
        return "❌ Пользователь не найден."

    # Если рейтинг слишком низкий — требуем прислать скриншот лайка
    if rating <= 0:
        conn.close()
        return (
            "⚠️ Ваш рейтинг слишком низкий. "
            "Пожалуйста, отправьте скриншот лайка для подтверждения."
        )

    # Получаем текущее задание
    cur.execute("SELECT links, task_time, task_done FROM tasks WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return "❌ Нет активных заданий."

    links, task_time, task_done = row
    link_list = links.split(",")

    if task_done >= len(link_list):
        conn.close()
        return "✅ Все задания уже подтверждены."

    elapsed = time.time() - task_time

    if elapsed < 30:
        new_warnings = warnings + 1

        if new_warnings >= 3:
            # Баним пользователя
            cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            return "⛔ Вы заблокированы за многократное нарушение таймера подтверждения лайков."

        else:
            # Обновляем предупреждения и уменьшаем рейтинг (не меньше 0)
            new_rating = max(0, rating - 1)
            cur.execute("UPDATE users SET warnings=?, rating=? WHERE user_id=?", (new_warnings, new_rating, user_id))
            conn.commit()
            conn.close()
            return (
                f"⏱ Пожалуйста, подожди минимум 30 секунд. Осталось {int(30 - elapsed)} сек.\n"
                f"⚠️ Предупреждение {new_warnings}/3 за слишком раннее подтверждение.\n"
                f"📉 Рейтинг снижен до {new_rating}."
            )

    # Если прошло 30 секунд — сбрасываем warnings и повышаем рейтинг
    if warnings != 0:
        new_rating = rating + 1
        cur.execute("UPDATE users SET warnings=0, rating=? WHERE user_id=?", (new_rating, user_id))
    else:
        new_rating = rating  # рейтинг не меняется, если warnings == 0

    # Засчитываем лайк
    current_link = link_list[task_done]
    cur.execute("INSERT OR IGNORE INTO likes_log (user_id, video_link) VALUES (?, ?)", (user_id, current_link))

    # Обновляем статистику лайков владельца видео
    cur.execute("SELECT user_id FROM videos WHERE link=?", (current_link,))
    owner_row = cur.fetchone()
    if owner_row:
        owner_id = owner_row[0]
        cur.execute("UPDATE users SET likes_received = likes_received + 1 WHERE user_id=?", (owner_id,))
        cur.execute("UPDATE users SET likes_given = likes_given + 1 WHERE user_id=?", (user_id,))

        cur.execute("SELECT COUNT(*) FROM likes_log WHERE video_link=?", (current_link,))
        total_likes = cur.fetchone()[0]

        if total_likes >= 3:
            cur.execute("DELETE FROM videos WHERE link=?", (current_link,))
            cur.execute("INSERT OR IGNORE INTO tasks (user_id, links, task_time, task_done) VALUES (?, ?, 0, 3)", (owner_id, current_link))

    # Обновляем прогресс
    task_done += 1
    now = time.time()

    if task_done >= len(link_list):
        cur.execute("DELETE FROM tasks WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return f"✅ Все задания выполнены! Твоё видео снова в очереди на 3 лайка! 🎉\n📈 Текущий рейтинг: {new_rating}"

    # Сохраняем прогресс
    cur.execute("UPDATE tasks SET task_done=?, task_time=? WHERE user_id=?", (task_done, now, user_id))
    conn.commit()
    conn.close()

    next_link = link_list[task_done]
    return (
        f"✅ Лайк засчитан!\n\n"
        f"🔗 Следующее видео:\n{next_link}\n\n"
        f"⏳ Жди 30 секунд и нажми ✅ Подтвердить лайки\n"
        f"📊 Прогресс: {task_done + 1} из {len(link_list)}\n"
        f"📈 Текущий рейтинг: {new_rating}"
    )

def get_top(limit=5):
    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()
    cur.execute("SELECT user_id, likes_given, likes_received FROM users ORDER BY likes_given DESC LIMIT ?", (limit,))
    top = cur.fetchall()
    conn.close()
    return top

def unblock_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    if not context.args:
        update.message.reply_text("❗ Укажите ID пользователя. Пример:\n/unblock 123456789")
        return

    try:
        unblock_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("❗ Неверный формат ID.")
        return

    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()
    cur.execute("UPDATE users SET banned=0, warnings=0 WHERE user_id=?", (unblock_id,))
    conn.commit()
    conn.close()

    update.message.reply_text(f"✅ Пользователь {unblock_id} разблокирован.")

def banned_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
    cur = conn.cursor()
    cur.execute("SELECT user_id, warnings FROM users WHERE banned=1")
    banned = cur.fetchall()
    conn.close()

    if not banned:
        update.message.reply_text("✅ Нет заблокированных пользователей.")
    else:
        msg = "<b>🚫 Заблокированные пользователи:</b>\n\n"
        for uid, w in banned:
            msg += f"🔒 ID <code>{uid}</code> — {w} предупреждений\n"
        update.message.reply_text(msg, parse_mode="HTML")

def start(update: Update, context: CallbackContext):
    print("Команда /start вызвана")

    try:
        user = update.effective_user
        print(f"User ID: {user.id}")
        chat_id = update.effective_chat.id
        print(f"Chat ID: {chat_id}")

        invited_by = None
        args = context.args
        if args and args[0].isdigit():
            candidate = int(args[0])
            if candidate != user.id:
                invited_by = candidate

        with db_lock:
            register_user(user.id, invited_by)

        keyboard = [
            [KeyboardButton("🔗 Добавить видео")],
            [KeyboardButton("📋 Получить задания"), KeyboardButton("✅ Подтвердить лайки")],
            [KeyboardButton("📊 Топ участников"), KeyboardButton("📜 Правила")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        welcome_text = (
            "✋🏻 Добро пожаловать! Используй кнопки ниже 👇\n\n"
            "ℹ️ Важно:\n"
            "- В начале для проверки честности заданий нужно присылать скриншоты лайков.\n"
            "- За подтвержденные скриншоты ваш рейтинг повышается, и вы сможете работать без скриншотов.\n"
            "- При подозрениях в обмане скриншоты снова станут обязательными.\n"
            "- За нечестное поведение (фальшивые скриншоты, мошенничество) возможна блокировка с удалением данных.\n\n"
            "Будьте честны — это выгодно всем!\n"
            "Удачи и приятного пользования!"
        )

        context.bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            reply_markup=reply_markup
        )
        print("✅ Сообщение /start отправлено.")

    except Exception as e:
        import traceback
        print("❌ Ошибка в функции start():")
        traceback.print_exc()

def handle_invite(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    register_user(user_id)
    referral_link = f"https://t.me/{context.bot.username}?start={user_id}"
    update.message.reply_text(
        f"👥 Пригласи друга и получи бонус!\n\n"
        f"Вот твоя ссылка:\n{referral_link}\n\n"
        f"Как только друг выполнит своё первое задание, твоё видео можно будет добавить без лайков!"
    )

def handle_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    register_user(user_id)

    # Админские команды
    if text.startswith("/unblock") and user_id == ADMIN_ID:
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            unblock_id = int(parts[1])
            conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
            cur = conn.cursor()
            cur.execute("UPDATE users SET banned=0, warnings=0 WHERE user_id=?", (unblock_id,))
            conn.commit()
            conn.close()
            update.message.reply_text(f"✅ Пользователь {unblock_id} разблокирован и предупреждения сброшены.")
        else:
            update.message.reply_text("❌ Используй команду так: /unblock ID")
        return

    if text == "/banned" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT user_id, warnings FROM users WHERE banned=1")
        banned = cur.fetchall()
        conn.close()
        if not banned:
            update.message.reply_text("✅ Нет заблокированных пользователей.")
        else:
            msg = "<b>🚫 Заблокированные пользователи:</b>\n\n"
            for uid, w in banned:
                msg += f"🔒 ID <code>{uid}</code> — {w} предупреждений\n"
            update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "/admin_stats" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM videos")
        total_videos = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tasks")
        active_tasks = cur.fetchone()[0]
        cur.execute("SELECT user_id, likes_given, likes_received FROM users ORDER BY likes_given DESC LIMIT 20")
        top = cur.fetchall()
        cur.execute("SELECT invited_by, COUNT(*) FROM users WHERE invited_by IS NOT NULL GROUP BY invited_by ORDER BY COUNT(*) DESC")
        invites = cur.fetchall()
        conn.close()
        msg = f"""📊 <b>Статистика бота:</b>

👥 Пользователей: <b>{total_users}</b>
🎞 Видео в очереди: <b>{total_videos}</b>
🧩 Активных заданий: <b>{active_tasks}</b>

🏆 <b>Топ 20 участников:</b>
"""
        for i, (uid, given, received) in enumerate(top, start=1):
            msg += f"{i}. ID <code>{uid}</code> — 👍🏻 {given} / ❤️ {received}\n"
        msg += "\n👥 <b>Реферальный отчёт:</b>\n"
        for inviter, count in invites:
            msg += f"ID <code>{inviter}</code> пригласил {count} человек(а)\n"
        update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "/invites" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT invited_by, COUNT(*) FROM users WHERE invited_by IS NOT NULL GROUP BY invited_by ORDER BY COUNT(*) DESC")
        results = cur.fetchall()
        conn.close()
        if not results:
            update.message.reply_text("📭 Пока нет приглашённых пользователей.")
            return
        msg = "<b>👥 Приглашения:</b>\n\n"
        for inviter_id, count in results:
            msg += f"🔸 ID <code>{inviter_id}</code> — пригласил <b>{count}</b> пользователей\n"
        update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "/video" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT id, link FROM videos")
        videos = cur.fetchall()
        conn.close()
        if not videos:
            update.message.reply_text("Очередь пуста.")
        else:
            msg = "\n".join([f"{v[0]}. {v[1]}" for v in videos])
            msg += "\n\nЧтобы удалить видео, напиши /delete ID"
            send_long_message(update, msg)
        return

    if text.startswith("/delete") and user_id == ADMIN_ID:
        parts = text.split()
        if len(parts) < 2:
            update.message.reply_text("❌ Используй: /delete ID [ID2 ID3...]")
            return

        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        deleted = []

        for pid in parts[1:]:
            if pid.isdigit():
                video_id = int(pid)
                cur.execute("DELETE FROM videos WHERE id=?", (video_id,))
                if cur.rowcount > 0:
                    deleted.append(video_id)

        conn.commit()
        conn.close()

        if deleted:
            update.message.reply_text(f"🗑 Удалены видео с ID: {', '.join(map(str, deleted))}")
        else:
            update.message.reply_text("❌ Ничего не удалено (возможно, ID не найдены)")
        return

    if text == "/test_notify" and user_id == ADMIN_ID:
        context.bot.send_message(chat_id=user_id, text="🔔 Это тестовое уведомление о новом задании.")
        return

    if text == "/invite":
        update.message.reply_text(f"🎁 Пригласи друга и получи 1 бесплатную загрузку видео!\nТвоя ссылка:\nhttps://t.me/{context.bot.username}?start={user_id}")
        return

    if text == "📜 Правила":
        update.message.reply_text(
            "📋 <b>Правила:</b>\n\n"
            "1. Отправь ссылку на своё видео с TikTok 🔗\n"
            "2. Получи 3 задания с чужими видео 📋\n"
            "3. Поставь на каждое видео ❤️ и ПОСМОТРИ каждое видео не менее 30 секунд\n"
            "4. Подтверди, что лайкнул! ВНИМАНИЕ‼️ Бот следит за нечестным выполнением заданий и блокирует пользователя в случае обнаружения нарушений ПЕРМАНЕНТНО. Будь ЧЕСТНЫМ ✅\n"
            "5. После подтверждения твоё видео добавится в очередь\n\n"
            "6. Оно будет удалено через 24 часа\n\n"
            "7. Как только оно получит 3 лайка, оно вновь пропадает из очереди и чтобы его вернуть обратно, нужно выполнить еще 3 задания\n\n"
            "8. Сколько поставил лайков ты, столько же получаешь в ответ! Все честно!🤗\n\n"
            "🎁 <b>Хочешь пропустить задания?</b>\nПригласи друга — и твоё видео сразу попадёт в очередь!\nКоманда: /invite\n\n"
            "📊 Статистика по кнопке 📊\n👮 Нечестные пользователи блокируются\n❓ Вопросы и разбан — @mihei_1985",
            parse_mode="HTML"
        )
        return

    if text == "🔗 Добавить видео":
        update.message.reply_text("🔗 Пришли ссылку на TikTok-видео")
        return

    if is_tiktok_link(text):
        is_admin = user_id == ADMIN_ID
        result = add_video(user_id, text, is_admin=is_admin)
        update.message.reply_text(result)
        return

    if text == "📋 Получить задания":
        tasks, done = get_tasks(user_id)

        if not tasks or done >= len(tasks):
            update.message.reply_text("📭 Нет доступных заданий. Попробуй позже.")
            return

        current_link = tasks[done]
        print(f"DEBUG: current_link = {current_link}")

        wrapped_link = current_link
        print(f"DEBUG: wrapped_link = {wrapped_link}")

        # обновляем время задачи
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        cur.execute("UPDATE tasks SET task_time=? WHERE user_id=?", (time.time(), user_id))
        conn.commit()
        conn.close()

        update.message.reply_text(
            f"👍 Поставь лайк этому видео:\n\n"
            f"🔗 <a href=\"{wrapped_link}\">Нажми здесь, чтобы открыть видео</a>\n\n"
            f"📱 Если открылось в браузере — нажми ⋮ и выбери <b>“Открыть в приложении TikTok”</b>\n\n"
            f"⏳ Жди минимум 30 секунд, затем нажми <b>✅ Подтвердить лайки</b>\n"
            f"📊 Прогресс: {done + 1} из {len(tasks)}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return

    if text == "✅ Подтвердить лайки":
        result = confirm_likes(user_id)
        update.message.reply_text(result)
        return

    if text == "📊 Топ участников":
        top = get_top(limit=20)
        if not top:
            update.message.reply_text("😢 Пока нет участников.")
        else:
            msg = "🏆 <b>Топ участников:</b>\n\n"
            for i, (uid, given, received) in enumerate(top, start=1):
                msg += f"{i}. ID {uid} — 👍🏻 {given} / ❤️ {received}\n"
            update.message.reply_text(msg, parse_mode="HTML")
        return

    update.message.reply_text("❓ Неизвестная команда. Нажми /start или выбери кнопку ниже.")

def auto_delete_screenshots(bot):
    import time
    import sqlite3

    while True:
        time.sleep(3600)  # Проверять каждый час
        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()
        now = time.time()
        cutoff = now - 86400  # 24 часа назад

        cur.execute("SELECT user_id, file_id FROM screenshots WHERE timestamp < ?", (cutoff,))
        old_screens = cur.fetchall()

        for user_id, file_id in old_screens:
            try:
                bot.delete_message(chat_id=user_id, message_id=file_id)
            except Exception as e:
                print(f"[!] Ошибка удаления скриншота {file_id} у пользователя {user_id}: {e}")

        cur.execute("DELETE FROM screenshots WHERE timestamp < ?", (cutoff,))
        conn.commit()
        conn.close()

def auto_confirm_screenshots(bot, timeout=86400):  # 24 часа
    while True:
        time.sleep(3600)  # проверять каждый час

        now = time.time()
        cutoff = now - timeout

        conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
        cur = conn.cursor()

        cur.execute("SELECT id, user_id FROM screenshots WHERE timestamp < ?", (cutoff,))
        old_screens = cur.fetchall()

        for screenshot_id, user_id in old_screens:
            cur.execute("UPDATE users SET rating = rating + 1 WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM screenshots WHERE id=?", (screenshot_id,))
            try:
                bot.send_message(user_id, "✅ Ваш скриншот лайка автоматически подтверждён через 24 часа.")
            except Exception:
                pass

        conn.commit()
        conn.close()            

import threading

db_lock = threading.Lock()  # если у тебя нет, добавь глобально в код

def auto_notify_new_tasks(bot):
    notify_interval = 3600  # 1 час

    print("Авто-уведомления запущены")
    while True:
        try:
            print("Ждем 60 секунд перед очередной проверкой...")
            time.sleep(60)
            print("Начинаем проверку новых задач")

            with db_lock:
                conn = sqlite3.connect("likes_bot.db", check_same_thread=False, timeout=10)
                cur = conn.cursor()

                cur.execute("SELECT user_id FROM users")
                users = [row[0] for row in cur.fetchall()]
                print(f"Пользователей для проверки: {len(users)}")

                now = time.time()

                for user_id in users:
                    print(f"Проверяем пользователя {user_id}")

                    # Проверяем активное задание
                    cur.execute("SELECT links FROM tasks WHERE user_id=?", (user_id,))
                    row = cur.fetchone()
                    if row:
                        print(f"Пользователь {user_id} уже имеет активное задание, пропускаем.")
                        continue

                    # Проверяем время последнего уведомления
                    cur.execute("SELECT last_notify FROM notify_log WHERE user_id=?", (user_id,))
                    last_notify_row = cur.fetchone()
                    if last_notify_row and now - last_notify_row[0] < notify_interval:
                        print(f"Пользователь {user_id} получил уведомление недавно, пропускаем.")
                        continue

                    # Видео от других пользователей
                    cur.execute("SELECT link FROM videos WHERE user_id != ?", (user_id,))
                    all_links = set(row[0] for row in cur.fetchall())

                    # Видео, которые лайкал пользователь
                    cur.execute("SELECT video_link FROM likes_log WHERE user_id=?", (user_id,))
                    liked_links = set(row[0] for row in cur.fetchall())

                    available_links = all_links - liked_links

                    print(f"Пользователь {user_id}: доступно видео для заданий - {len(available_links)}")

                    if len(available_links) >= 3:
                        try:
                            bot.send_message(chat_id=user_id, text="📢 Доступно новое задание! Нажми 📋 Получить задания")
                            print(f"Уведомление отправлено пользователю {user_id}")
                            cur.execute("""
                                INSERT INTO notify_log (user_id, last_notify)
                                VALUES (?, ?)
                                ON CONFLICT(user_id) DO UPDATE SET last_notify=excluded.last_notify
                            """, (user_id, now))
                            conn.commit()
                        except Exception as e:
                            print(f"Ошибка при отправке уведомления {user_id}: {e}")

                conn.close()

        except Exception as e:
            print(f"[auto_notify_new_tasks] Ошибка в цикле: {e}")

def safe_handler(func):
    def wrapper(update, context):
        try:
            func(update, context)
        except Exception as e:
            print(f"Ошибка в обработчике {func.__name__}: {e}")
    return wrapper

def main():
    keep_alive()
    init_db()

    # 🔄 Запускаем автоудаление старых видео каждые 10 минут
    def run_cleanup():
        while True:
            try:
                cleanup_old_videos()
            except Exception as e:
                print(f"Ошибка в cleanup_old_videos: {e}")
            time.sleep(600)

    threading.Thread(target=run_cleanup, daemon=True).start()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Запускаем фоновое уведомление о заданиях с обработкой ошибок
    def safe_thread(target_func, *args):
        def wrapper():
            try:
                target_func(*args)
            except Exception as e:
                print(f"Ошибка в фоновом потоке {target_func.__name__}: {e}")
        threading.Thread(target=wrapper, daemon=True).start()

    safe_thread(auto_notify_new_tasks, updater.bot)
    safe_thread(auto_delete_screenshots, updater.bot)
    safe_thread(auto_confirm_screenshots, updater.bot)

    # Обработчики команд и сообщений
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("invite", handle_invite))
    dp.add_handler(CommandHandler("video", handle_message))
    dp.add_handler(CommandHandler("admin_stats", handle_message))
    dp.add_handler(CommandHandler("delete", handle_message))
    dp.add_handler(CommandHandler("test_notify", handle_message))
    dp.add_handler(CommandHandler("invites", handle_message))
    dp.add_handler(CommandHandler("unblock", unblock_command))
    dp.add_handler(CommandHandler("banned", banned_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(MessageHandler(Filters.photo, handle_photo))
    dp.add_handler(CommandHandler("check_screenshots", handle_check_screenshots))
    dp.add_handler(CommandHandler("delete_screen", delete_screen_command))

    # CallbackQuery универсальный обработчик
    def callback_handler(update: Update, context: CallbackContext):
        query = update.callback_query
        data = query.data
        try:
            if data.startswith("screenshot_"):
                handle_screenshot_callback(update, context)
            else:
                handle_callback_query(update, context)
        except Exception as e:
            print(f"Ошибка в callback_handler: {e}")

    dp.add_handler(CallbackQueryHandler(callback_handler))

    # Команды для пользователей
    updater.bot.set_my_commands([
        BotCommand("start", "Начать работу"),
        BotCommand("invite", "👥 Пригласи друга и получи бонус 🎁"),
    ], scope=BotCommandScopeDefault())

    # Команды для админа
    updater.bot.set_my_commands([
        BotCommand("start", "🚀 Начать работу"),
        BotCommand("admin_stats", "📊 Статистика"),
        BotCommand("video", "📼 Список видео"),
        BotCommand("delete", "🗑 Удалить видео"),
        BotCommand("test_notify", "🔔 Тест уведомление"),
        BotCommand("invite", "👥 Пригласи друга и получи бонус 🎁"),
        BotCommand("invites", "👥 Приглашения пользователей"),
        BotCommand("check_screenshots", "🖼 Проверка скринов"),
        BotCommand("unblock", "🔓 Разблокировать пользователя"),
        BotCommand("banned", "🚫 Список заблокированных"),
    ], scope=BotCommandScopeChat(chat_id=str(ADMIN_ID)))

    updater.bot.delete_webhook()
    print("✅ Webhook удалён")

    updater.start_polling()
    print("🤖 Бот запущен и ждёт сообщения...")
    updater.idle()

# ✅ Безопасный запуск
if __name__ == '__main__':
    print("Запускаем main()…")
    try:
        main()
    except Exception as e:
        print("❌ Критическая ошибка при запуске main():")
        import traceback
        print(traceback.format_exc())




