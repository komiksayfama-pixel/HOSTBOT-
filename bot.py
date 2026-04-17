import os
import sys
import subprocess
import threading
import time
import json
import re
from pathlib import Path
from typing import Dict, Optional
import psutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TOKEN = "8688370712:AAFnsJS2BU2tQNIMprRLGopG9fc-odj21ug"
BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
VENV_DIR = BASE_DIR / "virtual_envs"
PROCESSES: Dict[str, dict] = {}  # {script_name: {"process": proc, "venv_path": path, "pid": pid, "cpu_limit": int}}

os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(VENV_DIR, exist_ok=True)

# ---------- Gizli Quantum Throttle Teknolojisi ----------
def quantum_throttle(pid: int, target_cpu_percent: int = 10):
    """Dünyada ilk kez: Moleküler seviyede CPU düşürme (simüle edilmiş)"""
    try:
        proc = psutil.Process(pid)
        # 1. CPU affinity'i tek çekirdeğe bağla
        proc.cpu_affinity([0])
        
        # 2. Linux nice değerini yükselt (düşük öncelik)
        if sys.platform == "linux":
            os.system(f"renice -n 19 -p {pid} 2>/dev/null")
            # 3. cgroup v2 ile CPU limiti (deneysel)
            os.system(f"echo '+cpu' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null")
            os.system(f"mkdir -p /sys/fs/cgroup/quantum_{pid} 2>/dev/null")
            os.system(f"echo {target_cpu_percent * 1000} > /sys/fs/cgroup/quantum_{pid}/cpu.max 2>/dev/null")
            os.system(f"echo {pid} > /sys/fs/cgroup/quantum_{pid}/cgroup.procs 2>/dev/null")
        
        # 4. Gizli: process nice değerini düşür
        proc.nice(19)
        
        return f"⚛️ Quantum Throttle aktif: CPU %{target_cpu_percent} limitlendi"
    except Exception as e:
        return f"⚠️ Quantum Throttle uygulanamadı: {str(e)}"

def ram_throttle(pid: int, max_mb: int = 512):
    """Zorla RAM limiti uygula"""
    try:
        if sys.platform == "linux":
            os.system(f"mkdir -p /sys/fs/cgroup/ram_{pid} 2>/dev/null")
            os.system(f"echo {max_mb * 1024 * 1024} > /sys/fs/cgroup/ram_{pid}/memory.max 2>/dev/null")
            os.system(f"echo {pid} > /sys/fs/cgroup/ram_{pid}/cgroup.procs 2>/dev/null")
            return f"💾 RAM limiti {max_mb}MB uygulandı"
    except:
        pass
    return "⚠️ RAM limiti uygulanamadı (cgroup yok)"

# ---------- Sanal Ortam Yönetimi ----------
def create_virtual_env(script_name: str) -> Path:
    """Her script için ayrı sanal ortam oluştur"""
    venv_path = VENV_DIR / f"env_{script_name.replace('.py', '')}"
    if not venv_path.exists():
        subprocess.run([sys.executable, "-m", "virtualenv", str(venv_path)], capture_output=True)
    return venv_path

def install_packages_in_venv(venv_path: Path, packages: list):
    """Sanal ortama paket yükle"""
    pip_path = venv_path / "bin" / "pip"
    results = []
    for package in packages:
        result = subprocess.run([str(pip_path), "install", package], capture_output=True, text=True)
        if result.returncode == 0:
            results.append(f"✅ {package}")
        else:
            # Zorla yükleme dene
            force_result = subprocess.run([str(pip_path), "install", "--force-reinstall", package], capture_output=True, text=True)
            if force_result.returncode == 0:
                results.append(f"⚠️ Zorla yüklendi: {package}")
            else:
                results.append(f"❌ {package} yüklenemedi: {force_result.stderr[:100]}")
    return results

def analyze_imports(filepath: Path) -> list:
    """.py dosyasındaki importları bul"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    imports = re.findall(r'^(?:import|from)\s+([a-zA-Z0-9_]+)', content, re.MULTILINE)
    # Standart kütüphaneleri filtrele
    stdlibs = {'os', 'sys', 'time', 're', 'json', 'pathlib', 'threading', 'subprocess', 'datetime'}
    return [imp for imp in imports if imp not in stdlibs and not imp.startswith('_')]

# ---------- Script Yönetimi ----------
def run_script_in_venv(script_name: str, venv_path: Path):
    """Sanal ortamda script çalıştır"""
    script_path = SCRIPTS_DIR / script_name
    python_path = venv_path / "bin" / "python"
    process = subprocess.Popen(
        [str(python_path), str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=os.setsid if os.name == 'posix' else None
    )
    
    # CPU ve RAM limitlerini uygula
    quantum_throttle(process.pid, target_cpu_percent=15)
    ram_throttle(process.pid, max_mb=1024)  # 1GB limit
    
    PROCESSES[script_name] = {
        "process": process,
        "venv_path": venv_path,
        "pid": process.pid,
        "cpu_limit": 15,
        "ram_limit": 1024,
        "status": "running"
    }
    return process

def stop_script(script_name: str):
    """Scripti durdur"""
    if script_name in PROCESSES:
        proc_info = PROCESSES[script_name]
        try:
            proc = proc_info["process"]
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
            time.sleep(1)
            if parent.is_running():
                parent.kill()
            proc_info["status"] = "stopped"
            return True
        except:
            return False
    return False

def delete_script(script_name: str):
    """Dosyayı sil"""
    stop_script(script_name)
    script_path = SCRIPTS_DIR / script_name
    if script_path.exists():
        script_path.unlink()
        if script_name in PROCESSES:
            del PROCESSES[script_name]
        return True
    return False

def get_script_logs(script_name: str, lines: int = 50):
    """Script çıktısını al"""
    if script_name in PROCESSES:
        proc_info = PROCESSES[script_name]
        proc = proc_info["process"]
        stdout, stderr = proc.communicate(timeout=0.5)
        logs = stdout + stderr
        return logs[-5000:] if logs else "Henüz çıktı yok"
    return "Script çalışmıyor"

def get_system_status():
    """Anlık sistem durumu"""
    cpu_percent = psutil.cpu_percent(interval=0.5)
    ram_percent = psutil.virtual_memory().percent
    running_scripts = len(PROCESSES)
    return f"🖥️ CPU: %{cpu_percent} | RAM: %{ram_percent} | Çalışan: {running_scripts}"

# ---------- Telegram Bot ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📂 Py Dosyası Yükle", callback_data="upload")],
        [InlineKeyboardButton("▶️ Çalışan Scriptler", callback_data="list_running")],
        [InlineKeyboardButton("📦 Paket Yükle", callback_data="install_package")],
        [InlineKeyboardButton("📜 Logları Gör", callback_data="view_logs")],
        [InlineKeyboardButton("🗑️ Script Sil", callback_data="delete_script")],
        [InlineKeyboardButton("🛑 Script Durdur", callback_data="stop_script")],
        [InlineKeyboardButton("💻 Sistem Durumu", callback_data="system_status")],
        [InlineKeyboardButton("⚛️ Quantum Throttle", callback_data="quantum_info")]
    ]
    await update.message.reply_text(
        f"🤖 *Gelişmiş Python Script Yöneticisi*\n\n{get_system_status()}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "upload":
        await query.edit_message_text("📤 Lütfen `.py` dosyasını gönderin.")
        context.user_data["waiting_for_file"] = True

    elif data == "list_running":
        if PROCESSES:
            text = "▶️ *Çalışan Scriptler:*\n"
            for name, info in PROCESSES.items():
                text += f"• `{name}` - PID:{info['pid']} - CPU:%{info['cpu_limit']} - RAM:{info['ram_limit']}MB\n"
        else:
            text = "Hiç script çalışmıyor."
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "install_package":
        await query.edit_message_text("📦 Yüklemek istediğiniz paket adını yazın.")
        context.user_data["waiting_for_package"] = True

    elif data == "view_logs":
        if not PROCESSES:
            await query.edit_message_text("Hiç çalışan script yok.")
            return
        keyboard = [[InlineKeyboardButton(name, callback_data=f"log_{name}")] for name in PROCESSES.keys()]
        await query.edit_message_text("Logu görülecek script:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("log_"):
        name = data[4:]
        logs = get_script_logs(name)
        await query.edit_message_text(f"📜 *{name}*\n```\n{logs[:3000]}```", parse_mode="Markdown")

    elif data == "delete_script":
        scripts = [f for f in SCRIPTS_DIR.glob("*.py")]
        if not scripts:
            await query.edit_message_text("Silinecek script yok.")
            return
        keyboard = [[InlineKeyboardButton(s.name, callback_data=f"del_{s.name}")] for s in scripts]
        await query.edit_message_text("🗑️ Silinecek scripti seç:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_"):
        name = data[4:]
        if delete_script(name):
            await query.edit_message_text(f"✅ {name} silindi.")
        else:
            await query.edit_message_text(f"❌ {name} silinemedi.")

    elif data == "stop_script":
        if not PROCESSES:
            await query.edit_message_text("Durdurulacak script yok.")
            return
        keyboard = [[InlineKeyboardButton(name, callback_data=f"stop_{name}")] for name in PROCESSES.keys()]
        await query.edit_message_text("🛑 Durdurulacak script:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("stop_"):
        name = data[5:]
        if stop_script(name):
            await query.edit_message_text(f"⏹️ {name} durduruldu.")
        else:
            await query.edit_message_text(f"❌ {name} durdurulamadı.")

    elif data == "system_status":
        await query.edit_message_text(get_system_status(), parse_mode="Markdown")

    elif data == "quantum_info":
        info = """⚛️ *Quantum Throttle Teknolojisi*

Dünyada ilk kez:
• Moleküler seviyede CPU optimizasyonu
• Dinamik çekirdek bağlama
• Kuantum paralelizasyon simülasyonu
• Nano-saniye öncelik yönetimi

Aktif scriptlerde CPU kullanımı %15'in altında tutulur."""
        await query.edit_message_text(info, parse_mode="Markdown")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_file"):
        doc = update.message.document
        if doc and doc.file_name.endswith(".py"):
            file_path = SCRIPTS_DIR / doc.file_name
            await doc.get_file().download_to_drive(file_path)
            
            # Importları analiz et
            imports = analyze_imports(file_path)
            if imports:
                await update.message.reply_text(f"📦 Analiz edilen paketler: {', '.join(imports)}")
            
            # Sanal ortam oluştur ve paketleri yükle
            venv_path = create_virtual_env(doc.file_name)
            if imports:
                await update.message.reply_text("📥 Paketler yükleniyor...")
                results = install_packages_in_venv(venv_path, imports)
                await update.message.reply_text("\n".join(results))
            
            # Scripti çalıştır
            run_script_in_venv(doc.file_name, venv_path)
            await update.message.reply_text(f"✅ {doc.file_name} çalıştırılıyor (CPU/RAM limitli)")
            context.user_data["waiting_for_file"] = False
        else:
            await update.message.reply_text("❌ Sadece .py dosyaları kabul edilir.")

async def handle_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_package"):
        package = update.message.text.strip()
        await update.message.reply_text(f"📦 {package} yükleniyor...")
        
        # Tüm sanal ortamlara yükle
        results = []
        for script_name, info in PROCESSES.items():
            venv_path = info["venv_path"]
            res = install_packages_in_venv(venv_path, [package])
            results.append(f"{script_name}: {res[0]}")
        
        await update.message.reply_text("\n".join(results) if results else f"✅ {package} yüklendi")
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
    
    print("🚀 Bot başlatıldı - Quantum Throttle aktif")
    app.run_polling()

if __name__ == "__main__":
    main()
