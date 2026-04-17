import os
import subprocess
import sys
import threading
import time
import psutil
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TOKEN = "8688370712:AAFnsJS2BU2tQNIMprRLGopG9fc-odj21ug"
UPLOAD_DIR = "uploaded_scripts"
PROCESSES = {}  # {filename: process}

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------- Yardımcı Fonksiyonlar ----------
def kill_process(pid):
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=3)
    except:
        try:
            proc.kill()
        except:
            pass

def run_script(filename):
    filepath = os.path.join(UPLOAD_DIR, filename)
    process = subprocess.Popen([sys.executable, filepath], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    PROCESSES[filename] = process
    return process

def get_logs(filename):
    process = PROCESSES.get(filename)
    if process:
        stdout, stderr = process.communicate(timeout=1)
        return stdout + stderr
    return "Process not running"

def install_package(package):
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", package], check=True, capture_output=True, text=True)
        return f"✅ {package} başarıyla yüklendi."
    except subprocess.CalledProcessError as e:
        return f"❌ Hata: {e.stderr}"

def force_install_package(package):
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--force-reinstall", package], check=True, capture_output=True, text=True)
        return f"⚠️ Zorla yüklendi: {package}"
    except subprocess.CalledProcessError as e:
        return f"❌ Zorla yükleme başarısız: {e.stderr}"

# ---------- Bot Komutları ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📂 Py Dosyası Yükle", callback_data="upload")],
        [InlineKeyboardButton("▶️ Çalışan Scriptler", callback_data="list_running")],
        [InlineKeyboardButton("📦 Paket Yükle", callback_data="install_package")],
        [InlineKeyboardButton("📜 Logları Gör", callback_data="view_logs")],
        [InlineKeyboardButton("🛑 Tümünü Durdur", callback_data="stop_all")]
    ]
    await update.message.reply_text("🤖 *Python Script Yöneticisi*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "upload":
        await query.edit_message_text("Lütfen `.py` dosyasını gönderin.")
        context.user_data["waiting_for_file"] = True

    elif data == "list_running":
        if PROCESSES:
            text = "▶️ *Çalışan Scriptler:*\n"
            for name, proc in PROCESSES.items():
                status = "Çalışıyor" if proc.poll() is None else "Bitti"
                text += f"• `{name}` - PID: {proc.pid} - {status}\n"
        else:
            text = "Hiç script çalışmıyor."
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "install_package":
        await query.edit_message_text("Yüklemek istediğiniz paket adını yazın.")
        context.user_data["waiting_for_package"] = True

    elif data == "view_logs":
        if not PROCESSES:
            await query.edit_message_text("Hiç çalışan script yok.")
            return
        keyboard = []
        for name in PROCESSES.keys():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"log_{name}")])
        await query.edit_message_text("Logunu görmek istediğin scripti seç:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("log_"):
        filename = data[4:]
        logs = get_logs(filename)
        await query.edit_message_text(f"📜 *{filename}*\n```\n{logs[:3000]}```", parse_mode="Markdown")

    elif data == "stop_all":
        for name, proc in list(PROCESSES.items()):
            kill_process(proc.pid)
            del PROCESSES[name]
        await query.edit_message_text("✅ Tüm scriptler durduruldu.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_file"):
        document = update.message.document
        if document and document.file_name.endswith(".py"):
            file = await document.get_file()
            file_path = os.path.join(UPLOAD_DIR, document.file_name)
            await file.download_to_drive(file_path)

            # Otomatik paket yükleme (importları analiz et)
            with open(file_path, "r") as f:
                content = f.read()
            imports = [line.split()[1].split(".")[0] for line in content.splitlines() if line.startswith("import ") or line.startswith("from ")]
            unique_imports = set(imports)
            for pkg in unique_imports:
                if pkg not in sys.modules:
                    result = install_package(pkg)
                    if "başarısız" in result:
                        result = force_install_package(pkg)
                    await update.message.reply_text(result)

            # Scripti başlat
            run_script(document.file_name)
            await update.message.reply_text(f"✅ {document.file_name} yüklendi ve çalıştırılıyor.")
            context.user_data["waiting_for_file"] = False
        else:
            await update.message.reply_text("Lütfen geçerli bir `.py` dosyası gönderin.")

async def handle_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_package"):
        package = update.message.text.strip()
        result = install_package(package)
        if "başarısız" in result:
            result = force_install_package(package)
        await update.message.reply_text(result)
        context.user_data["waiting_for_package"] = False

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("İşlem iptal edildi.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_package))
    app.add_handler(CommandHandler("cancel", cancel))
    print("Bot çalışıyor...")
    app.run_polling()

if __name__ == "__main__":
    main()
