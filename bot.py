import os
import sys
import subprocess
import re
import time
import asyncio
from pathlib import Path
from typing import Dict
import psutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== KONFIGÜRASYON ==========
TOKEN = "8688370712:AAFnsJS2BU2tQNIMprRLGopG9fc-odj21ug"
BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
PROCESSES: Dict[str, dict] = {}

# Dizinleri oluştur
SCRIPTS_DIR.mkdir(exist_ok=True)

# ========== PAKET YÜKLEME (HIZLI + ZORLA) ==========
def install_package_fast(package: str) -> str:
    """Hızlı paket yükleme - önce normal, olmazsa zorla"""
    try:
        # Normal yükleme
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return f"✅ {package} yüklendi"
        
        # Zorla yükleme dene
        force_result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", package],
            capture_output=True, text=True, timeout=60
        )
        if force_result.returncode == 0:
            return f"⚠️ {package} zorla yüklendi"
        else:
            return f"❌ {package} yüklenemedi: {force_result.stderr[:100]}"
    except subprocess.TimeoutExpired:
        return f"⏰ {package} zaman aşımı"
    except Exception as e:
        return f"❌ {package} hatası: {str(e)}"

def detect_imports_from_code(code: str) -> list:
    """Koddan importları hızlıca algıla"""
    imports = set()
    lines = code.split('\n')
    
    for line in lines:
        line = line.strip()
        # import xyz
        if line.startswith('import '):
            parts = line[7:].split()
            if parts:
                imports.add(parts[0].split('.')[0])
        # from xyz import abc
        elif line.startswith('from '):
            parts = line[5:].split()
            if parts and parts[0] not in ['import', '.']:
                imports.add(parts[0].split('.')[0])
    
    # Python standart kütüphanelerini filtrele
    stdlib = {'os', 'sys', 'time', 're', 'json', 'pathlib', 'threading', 
              'subprocess', 'datetime', 'math', 'random', 'collections', 
              'itertools', 'functools', 'typing', 'socket', 'ssl', 'hashlib',
              'base64', 'uuid', 'tempfile', 'shutil', 'glob', 'argparse',
              'logging', 'queue', 'signal', 'asyncio', 'io', 'struct', 'enum',
              'abc', 'contextlib', 'copy', 'pprint', 'traceback', 'warnings'}
    
    return [p for p in imports if p not in stdlib and len(p) > 1]

# ========== SCRIPT YÖNETİMİ ==========
def run_script_fast(script_name: str) -> str:
    """Scripti hızlıca çalıştır"""
    script_path = SCRIPTS_DIR / script_name
    
    if not script_path.exists():
        return "❌ Dosya bulunamadı"
    
    # Zaten çalışıyor mu kontrol et
    if script_name in PROCESSES:
        proc_info = PROCESSES[script_name]
        if proc_info["process"].poll() is None:
            return f"⚠️ {script_name} zaten çalışıyor (PID: {proc_info['pid']})"
    
    try:
        # Scripti başlat
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True
        )
        
        PROCESSES[script_name] = {
            "process": process,
            "pid": process.pid,
            "status": "running",
            "start_time": time.time()
        }
        
        # CPU ve RAM optimizasyonu (opsiyonel)
        try:
            p = psutil.Process(process.pid)
            p.nice(10)  # Düşük öncelik
            if hasattr(p, "cpu_affinity"):
                p.cpu_affinity([0])  # Tek çekirdek
        except:
            pass
        
        return f"✅ {script_name} başlatıldı (PID: {process.pid})"
    except Exception as e:
        return f"❌ Başlatma hatası: {str(e)}"

def stop_script(script_name: str) -> str:
    """Scripti durdur"""
    if script_name not in PROCESSES:
        return f"❌ {script_name} çalışmıyor"
    
    proc_info = PROCESSES[script_name]
    try:
        process = proc_info["process"]
        
        # Önce nazikçe sonlandır
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Zorla sonlandır
            process.kill()
        
        # Alt süreçleri de temizle
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                child.terminate()
        except:
            pass
        
        del PROCESSES[script_name]
        return f"⏹️ {script_name} durduruldu"
    except Exception as e:
        return f"❌ Durdurma hatası: {str(e)}"

def delete_script(script_name: str) -> str:
    """Script dosyasını sil"""
    # Önce durdur
    stop_script(script_name)
    
    script_path = SCRIPTS_DIR / script_name
    if script_path.exists():
        script_path.unlink()
        return f"🗑️ {script_name} silindi"
    return f"❌ {script_name} bulunamadı"

def get_script_log(script_name: str) -> str:
    """Script çıktısını al"""
    if script_name not in PROCESSES:
        return "❌ Script çalışmıyor"
    
    proc_info = PROCESSES[script_name]
    process = proc_info["process"]
    
    try:
        stdout, stderr = process.communicate(timeout=0.5)
        output = stdout + stderr
        if output:
            return f"📜 {script_name} çıktısı:\n```\n{output[:2000]}```"
        else:
            return f"📜 {script_name} henüz çıktı vermedi"
    except subprocess.TimeoutExpired:
        return f"📜 {script_name} çalışıyor (henüz çıktı yok)"
    except:
        return f"⚠️ {script_name} log alınamadı"

def get_running_scripts() -> str:
    """Çalışan scriptleri listele"""
    if not PROCESSES:
        return "📭 Çalışan script yok"
    
    result = "📋 *Çalışan Scriptler:*\n\n"
    for name, info in PROCESSES.items():
        duration = int(time.time() - info["start_time"])
        result += f"• `{name}`\n  PID: {info['pid']} | Süre: {duration}s\n\n"
    return result

# ========== TELEGRAM BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📂 Gönder ve Çalıştır", callback_data="upload")],
        [InlineKeyboardButton("▶️ Çalışan Scriptler", callback_data="list")],
        [InlineKeyboardButton("🛑 Script Durdur", callback_data="stop_menu")],
        [InlineKeyboardButton("🗑️ Script Sil", callback_data="delete_menu")],
        [InlineKeyboardButton("📦 Paket Yükle", callback_data="package_menu")],
        [InlineKeyboardButton("💻 Sistem Durumu", callback_data="status")]
    ]
    
    await update.message.reply_text(
        "🚀 *Python Script Yöneticisi*\n\n"
        "📤 `.py` dosyası gönder, otomatik çalıştırsın!\n"
        "⚡ Hızlı paket yükleme | Anlık log görüntüleme",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "upload":
        await query.edit_message_text("📤 *Python dosyasını gönder* (sadece .py)\n\nHemen algılayıp çalıştıracağım.", parse_mode="Markdown")
        context.user_data["waiting_file"] = True
    
    elif data == "list":
        await query.edit_message_text(get_running_scripts(), parse_mode="Markdown")
    
    elif data == "stop_menu":
        if not PROCESSES:
            await query.edit_message_text("📭 Durdurulacak script yok")
            return
        keyboard = [[InlineKeyboardButton(name, callback_data=f"stop_{name}")] for name in PROCESSES.keys()]
        keyboard.append([InlineKeyboardButton("🔙 Geri", callback_data="back")])
        await query.edit_message_text("🛑 *Durdurulacak scripti seç:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data.startswith("stop_"):
        script_name = data[5:]
        result = stop_script(script_name)
        await query.edit_message_text(result, parse_mode="Markdown")
    
    elif data == "delete_menu":
        files = list(SCRIPTS_DIR.glob("*.py"))
        if not files:
            await query.edit_message_text("📭 Silinecek dosya yok")
            return
        keyboard = [[InlineKeyboardButton(f.name, callback_data=f"del_{f.name}")] for f in files[:10]]
        keyboard.append([InlineKeyboardButton("🔙 Geri", callback_data="back")])
        await query.edit_message_text("🗑️ *Silinecek dosyayı seç:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data.startswith("del_"):
        script_name = data[4:]
        result = delete_script(script_name)
        await query.edit_message_text(result, parse_mode="Markdown")
    
    elif data == "package_menu":
        await query.edit_message_text("📦 *Yüklemek istediğin paket adını yaz:*\n\nÖrnek: `requests` veya `numpy pandas`", parse_mode="Markdown")
        context.user_data["waiting_package"] = True
    
    elif data == "status":
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        text = f"💻 *Sistem Durumu*\n\n"
        text += f"🖥️ CPU: %{cpu}\n"
        text += f"💾 RAM: %{ram}\n"
        text += f"💽 Disk: %{disk}\n"
        text += f"📂 Scriptler: {len(list(SCRIPTS_DIR.glob('*.py')))}\n"
        text += f"▶️ Çalışan: {len(PROCESSES)}"
        await query.edit_message_text(text, parse_mode="Markdown")
    
    elif data == "back":
        await start(update, context)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dosyayı al, paketleri yükle, çalıştır"""
    if not context.user_data.get("waiting_file"):
        return
    
    document = update.message.document
    if not document or not document.file_name.endswith('.py'):
        await update.message.reply_text("❌ Lütfen geçerli bir `.py` dosyası gönder!")
        return
    
    # Dosyayı indir
    status_msg = await update.message.reply_text("📥 Dosya indiriliyor...")
    file_path = SCRIPTS_DIR / document.file_name
    file = await document.get_file()
    await file.download_to_drive(file_path)
    
    # Kodu oku ve importları bul
    await status_msg.edit_text("🔍 Kod analiz ediliyor...")
    with open(file_path, 'r', encoding='utf-8') as f:
        code = f.read()
    
    imports = detect_imports_from_code(code)
    
    # Paketleri yükle
    if imports:
        await status_msg.edit_text(f"📦 {len(imports)} paket yükleniyor...\n{', '.join(imports)}")
        results = []
        for package in imports:
            result = install_package_fast(package)
            results.append(result)
            await status_msg.edit_text(f"📦 Yükleniyor: {package}\n\n" + "\n".join(results[-3:]))
        await status_msg.edit_text("✅ Paketler tamamlandı!\n\n" + "\n".join(results))
    else:
        await status_msg.edit_text("✅ Paket gerekmiyor (sadece standart kütüphaneler)")
    
    # Scripti çalıştır
    await status_msg.edit_text("🚀 Script başlatılıyor...")
    result = run_script_fast(document.file_name)
    
    # Log göster
    await asyncio.sleep(1)
    log = get_script_log(document.file_name)
    
    await update.message.reply_text(
        f"{result}\n\n{log}",
        parse_mode="Markdown"
    )
    
    context.user_data["waiting_file"] = False

async def handle_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paket yükleme işlemi"""
    if not context.user_data.get("waiting_package"):
        return
    
    packages = update.message.text.strip().split()
    if not packages:
        await update.message.reply_text("❌ Paket adı yazmalısın!")
        return
    
    msg = await update.message.reply_text(f"📦 {len(packages)} paket yükleniyor...")
    results = []
    
    for package in packages:
        result = install_package_fast(package)
        results.append(result)
        await msg.edit_text("\n".join(results))
    
    await msg.edit_text("✅ *İşlem tamamlandı*\n\n" + "\n".join(results), parse_mode="Markdown")
    context.user_data["waiting_package"] = False

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ İşlem iptal edildi")

# ========== ANA FONKSİYON ==========
def main():
    """Botu başlat"""
    app = Application.builder().token(TOKEN).build()
    
    # Handler'lar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_package))
    app.add_handler(CommandHandler("cancel", cancel))
    
    print("""
    ╔════════════════════════════════════╗
    ║   🚀 PYTHON SCRIPT MANAGER ACTIVE  ║
    ║                                    ║
    ║   📤 .py dosyası gönderebilirsin   ║
    ║   ⚡ Hızlı paket yükleme hazır     ║
    ║   💾 CPU/RAM optimizasyonu aktif   ║
    ╚════════════════════════════════════╝
    """)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
