import asyncio
import os
import sys
import logging
import subprocess
import psutil
import sqlite3
import hashlib
import zipfile
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# KONFİG
TOKEN = "8688370712:AAGzqgKs4_ncJI59OrPKRKmqas18nWYB34k"
OWNER_ID =8461081198
ADMIN_ID = 8461081198
YOUR_USERNAME = "@MANSURIxGOD"
UPDATE_CHANNEL = "https://t.me/tech_zone_dev"

if not TOKEN:
    raise ValueError("BOT_TOKEN is missing!")

BASE_DIR = Path(__file__).parent.absolute()
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'
IROTECH_DIR = BASE_DIR / 'inf'
DATABASE_PATH = IROTECH_DIR / 'bot_data.db'

# LİMİTLER
FREE_USER_LIMIT = 20
SUBSCRIBED_USER_LIMIT = 50
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

UPLOAD_BOTS_DIR.mkdir(exist_ok=True)
IROTECH_DIR.mkdir(exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

bot_scripts = {}
user_subscriptions = {}
user_files = {}
user_favorites = {}
banned_users = set()
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
bot_stats = {'total_uploads': 0, 'total_downloads': 0, 'total_runs': 0}

def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_files
                 (user_id INTEGER, file_name TEXT, file_type TEXT, upload_date TEXT,
                  PRIMARY KEY (user_id, file_name))''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_users
                 (user_id INTEGER PRIMARY KEY, join_date TEXT, last_active TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins
                 (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                 (user_id INTEGER PRIMARY KEY, banned_date TEXT, reason TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (user_id INTEGER, file_name TEXT, PRIMARY KEY (user_id, file_name))''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_stats
                 (stat_name TEXT PRIMARY KEY, stat_value INTEGER)''')
    c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
    for stat in ['total_uploads', 'total_downloads', 'total_runs']:
        c.execute('INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES (?, 0)', (stat,))
    conn.commit()
    conn.close()

def load_data():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id, expiry FROM subscriptions')
    for user_id, expiry in c.fetchall():
        try:
            user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
        except:
            pass
    c.execute('SELECT user_id, file_name, file_type FROM user_files')
    for user_id, file_name, file_type in c.fetchall():
        if user_id not in user_files:
            user_files[user_id] = []
        user_files[user_id].append((file_name, file_type))
    c.execute('SELECT user_id FROM active_users')
    active_users.update(user_id for (user_id,) in c.fetchall())
    c.execute('SELECT user_id FROM admins')
    admin_ids.update(user_id for (user_id,) in c.fetchall())
    c.execute('SELECT user_id FROM banned_users')
    banned_users.update(user_id for (user_id,) in c.fetchall())
    c.execute('SELECT user_id, file_name FROM favorites')
    for user_id, file_name in c.fetchall():
        if user_id not in user_favorites:
            user_favorites[user_id] = []
        user_favorites[user_id].append(file_name)
    c.execute('SELECT stat_name, stat_value FROM bot_stats')
    for stat_name, stat_value in c.fetchall():
        bot_stats[stat_name] = stat_value
    conn.close()

init_db()
load_data()

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_main_keyboard(user_id):
    if user_id in admin_ids:
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("📢 Updates", url=UPDATE_CHANNEL),
            InlineKeyboardButton("📤 Upload", callback_data="upload_file"),
            InlineKeyboardButton("📁 My Files", callback_data="check_files"),
            InlineKeyboardButton("⭐ Favorites", callback_data="my_favorites"),
            InlineKeyboardButton("🔍 Search", callback_data="search_files"),
            InlineKeyboardButton("📊 Stats", callback_data="statistics"),
            InlineKeyboardButton("👑 Admin", callback_data="admin_panel"),
            InlineKeyboardButton("💬 Contact", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")
        )
    else:
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("📢 Updates", url=UPDATE_CHANNEL),
            InlineKeyboardButton("📤 Upload", callback_data="upload_file"),
            InlineKeyboardButton("📁 My Files", callback_data="check_files"),
            InlineKeyboardButton("⭐ Favorites", callback_data="my_favorites"),
            InlineKeyboardButton("🔍 Search", callback_data="search_files"),
            InlineKeyboardButton("📊 Stats", callback_data="statistics"),
            InlineKeyboardButton("💎 Premium", callback_data="get_premium"),
            InlineKeyboardButton("💬 Contact", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")
        )
    return keyboard

@dp.message_handler(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id in banned_users:
        await message.answer("🚫 You are banned!")
        return
    active_users.add(user_id)
    welcome_text = f"🌟 Welcome {message.from_user.full_name}!\n📦 Limit: {get_user_file_limit(user_id)} files\n💎 Account: {'Premium' if user_id in user_subscriptions else 'Free'}"
    await message.answer(welcome_text, reply_markup=get_main_keyboard(user_id))

@dp.callback_query_handler(text="back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text("Main Menu", reply_markup=get_main_keyboard(callback.from_user.id))

@dp.callback_query_handler(text="upload_file")
async def upload_file(callback: types.CallbackQuery):
    await callback.message.edit_text("Send me a .py, .js, or .zip file", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main")))

@dp.callback_query_handler(text="check_files")
async def check_files(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    if not files:
        await callback.message.edit_text("No files found!", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main")))
        return
    text = f"📁 Your Files ({len(files)}):\n\n"
    keyboard = InlineKeyboardMarkup(row_width=2)
    for file_name, file_type in files:
        text += f"📄 {file_name}\n"
        keyboard.add(InlineKeyboardButton(f"▶️ Run {file_name[:15]}", callback_data=f"run_{file_name}"))
        keyboard.add(InlineKeyboardButton(f"🗑️ Delete", callback_data=f"del_{file_name}"))
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main"))
    await callback.message.edit_text(text, reply_markup=keyboard)

@dp.callback_query_handler(text="my_favorites")
async def my_favorites(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    favs = user_favorites.get(user_id, [])
    if not favs:
        await callback.message.edit_text("No favorites!", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main")))
        return
    text = "⭐ Favorites:\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)
    for file_name in favs:
        text += f"📄 {file_name}\n"
        keyboard.add(InlineKeyboardButton(f"▶️ Run {file_name[:20]}", callback_data=f"run_{file_name}"))
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main"))
    await callback.message.edit_text(text, reply_markup=keyboard)

@dp.callback_query_handler(text="statistics")
async def statistics(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = f"📊 Your Stats:\nFiles: {len(user_files.get(user_id, []))}\nFavorites: {len(user_favorites.get(user_id, []))}\nPremium: {'Yes' if user_id in user_subscriptions else 'No'}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main")))

@dp.callback_query_handler(text="get_premium")
async def get_premium(callback: types.CallbackQuery):
    text = "💎 Premium Plan:\n- 50 files\n- Priority support\nContact: @MANSURIxGOD"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="back_to_main")))

@dp.callback_query_handler(text="admin_panel")
async def admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("Admin only!")
        return
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 Users", callback_data="admin_users"),
        InlineKeyboardButton("📁 Files", callback_data="admin_files"),
        InlineKeyboardButton("🚀 Scripts", callback_data="admin_scripts"),
        InlineKeyboardButton("🔒 Lock", callback_data="lock_bot"),
        InlineKeyboardButton("🔙 Back", callback_data="back_to_main")
    )
    await callback.message.edit_text("👑 Admin Panel", reply_markup=keyboard)

@dp.callback_query_handler(text="admin_users")
async def admin_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        return
    await callback.message.edit_text(f"Total Users: {len(active_users)}\nBanned: {len(banned_users)}", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel")))

@dp.callback_query_handler(text="admin_files")
async def admin_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        return
    total = sum(len(files) for files in user_files.values())
    await callback.message.edit_text(f"Total Files: {total}", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel")))

@dp.callback_query_handler(text="admin_scripts")
async def admin_scripts(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        return
    text = f"Running Scripts: {len(bot_scripts)}"
    keyboard = InlineKeyboardMarkup()
    for key in bot_scripts:
        keyboard.add(InlineKeyboardButton(f"🛑 Stop", callback_data=f"stop_{key}"))
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
    await callback.message.edit_text(text, reply_markup=keyboard)

@dp.callback_query_handler(text="lock_bot")
async def lock_bot(callback: types.CallbackQuery):
    global bot_locked
    if callback.from_user.id not in admin_ids:
        return
    bot_locked = not bot_locked
    await callback.answer(f"Bot {'Locked' if bot_locked else 'Unlocked'}!")

@dp.message_handler(content_types=['document'])
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    if user_id in banned_users or (bot_locked and user_id not in admin_ids):
        await message.answer("Access denied!")
        return
    doc = message.document
    file_name = doc.file_name
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ['.py', '.js', '.zip']:
        await message.answer("Only .py, .js, .zip files!")
        return
    current = len(user_files.get(user_id, []))
    limit = get_user_file_limit(user_id)
    if current >= limit:
        await message.answer(f"Limit reached! {current}/{limit}")
        return
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    user_folder.mkdir(exist_ok=True)
    file_path = user_folder / file_name
    await doc.download(destination=file_path)
    if user_id not in user_files:
        user_files[user_id] = []
    user_files[user_id].append((file_name, ext[1:]))
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
              (user_id, file_name, ext[1:], datetime.now().isoformat()))
    c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = "total_uploads"')
    conn.commit()
    conn.close()
    bot_stats['total_uploads'] += 1
    await message.answer(f"✅ Uploaded: {file_name}\n{current+1}/{limit} files")

@dp.callback_query_handler(text_startswith="run_")
async def run_script(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.replace("run_", "")
    file_path = UPLOAD_BOTS_DIR / str(user_id) / file_name
    if not file_path.exists():
        await callback.answer("File not found!")
        return
    script_key = f"{user_id}_{file_name}"
    if script_key in bot_scripts:
        await callback.answer("Already running!")
        return
    ext = file_path.suffix.lower()
    try:
        if ext == '.py':
            proc = subprocess.Popen([sys.executable, str(file_path)], cwd=str(file_path.parent))
        elif ext == '.js':
            proc = subprocess.Popen(['node', str(file_path)], cwd=str(file_path.parent))
        else:
            await callback.answer("Cannot run this file!")
            return
        bot_scripts[script_key] = {'process': proc, 'file_name': file_name, 'user_id': user_id, 'start_time': datetime.now()}
        bot_stats['total_runs'] += 1
        await callback.answer(f"✅ Started! PID: {proc.pid}")
    except Exception as e:
        await callback.answer(f"Error: {str(e)}")

@dp.callback_query_handler(text_startswith="stop_")
async def stop_script(callback: types.CallbackQuery):
    script_key = callback.data.replace("stop_", "")
    if script_key in bot_scripts:
        try:
            proc = bot_scripts[script_key]['process']
            proc.terminate()
            del bot_scripts[script_key]
            await callback.answer("✅ Stopped!")
        except:
            await callback.answer("Error stopping!")

@dp.callback_query_handler(text_startswith="del_")
async def delete_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.replace("del_", "")
    file_path = UPLOAD_BOTS_DIR / str(user_id) / file_name
    if file_path.exists():
        file_path.unlink()
    if user_id in user_files:
        user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
    conn.commit()
    conn.close()
    await callback.answer("✅ Deleted!")
    await check_files(callback)

@dp.message_handler(Command("search"))
async def search_files(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /search filename")
        return
    search = args[1].lower()
    files = user_files.get(user_id, [])
    matches = [f for f in files if search in f[0].lower()]
    if not matches:
        await message.answer(f"No files matching '{search}'")
        return
    text = f"🔍 Found {len(matches)} files:\n\n"
    for fname, ftype in matches:
        text += f"📄 {fname}\n"
    await message.answer(text)

@dp.message_handler(Command("stats"))
async def stats_cmd(message: types.Message):
    user_id = message.from_user.id
    await message.answer(f"📊 Your Stats:\nFiles: {len(user_files.get(user_id, []))}\nFavorites: {len(user_favorites.get(user_id, []))}")

@dp.message_handler(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer("/start - Start bot\n/search - Search files\n/stats - Your stats\n/help - This help")

async def main():
    logger.info("Bot starting...")
    await dp.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
