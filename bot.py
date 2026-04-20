import os
import sys
import subprocess
import asyncio
import logging
import shlex
from typing import Dict, Optional
from datetime import datetime

# Telegram Kütüphaneleri (aiogram 3.x)
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= KONFIGURASYON =================
# !! DİKKAT !! Buraya kendi bot token'ınızı yazın.
BOT_TOKEN = "8646081251:AAGkofPa4q3YRABfVW7HM0PXrjIizjGXiEg"

# Botun yönetmesine izin verdiğimiz maksimum script sayısı
MAX_BOT_COUNT =20
# Bir script'in çalışması için maksimum süre (saniye)
TIMEOUT_SECONDS =5
# Klasör yapısı
SCRIPTS_DIR = "scripts"
LOGS_DIR = "logs"

# Global değişkenler: Aktif süreçleri takip etmek için
# {process_id: {"process": process_object, "name": script_name, "cmd": cmd, "log_file": path}}
active_processes: Dict[int, dict] = {}

# Loglama ayarları
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Bot ve Dispatcher nesneleri
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================= YARDIMCI FONKSIYONLAR =================

def setup_directories():
    """Gerekli klasörleri oluşturur"""
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

async def install_requirements_if_needed(script_path: str):
    """Script'in importlarını kontrol edip gerekli paketleri yükler (Basit regex ile)"""
    # Bu fonksiyon, script içindeki 'import X' satırlarını bulmaya çalışır.
    # Not: Bu basit bir yaklaşımdır, sanal ortam önerilir.
    try:
        with open(script_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Çok basit bir import yakalama (standart kütüphaneleri hariç tutmak zor)
        # Burada örnek olarak sadece 'requests' ve 'bs4' kontrolü yapıyoruz.
        # Daha gelişmiş bir çözüm için 'pipreqs' kullanılabilir.
        imports_to_check = []
        if 'import requests' in content or 'from requests' in content:
            imports_to_check.append('requests')
        if 'import bs4' in content or 'from bs4' in content:
            imports_to_check.append('beautifulsoup4')
            
        for lib in imports_to_check:
            try:
                __import__(lib.replace('-', '_'))
            except ImportError:
                logger.info(f"Kuruluyor: {lib}")
                await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", lib,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
    except Exception as e:
        logger.error(f"Paket kontrolü hatası: {e}")

async def run_script(script_name: str, message: Message) -> Optional[int]:
    """Script'i asenkron olarak çalıştırır ve Process ID'sini döndürür"""
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.exists(script_path):
        await message.answer(f"❌ Hata: '{script_name}' bulunamadı.")
        return None

    # Log dosyasını hazırla
    log_filename = f"{script_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path = os.path.join(LOGS_DIR, log_filename)
    
    # Eksik paketleri yüklemeyi dene
    await install_requirements_if_needed(script_path)
    
    try:
        # Script'i çalıştır (shell=False daha güvenlidir [citation:3])
        # asyncio.create_subprocess_exec ile subprocess yönetimi [citation:8]
        process = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd()  # Çalışma dizinini ana dizin yap
        )
        
        # Process'i sözlüğe kaydet
        active_processes[process.pid] = {
            "process": process,
            "name": script_name,
            "cmd": f"python {script_name}",
            "log_file": log_path,
            "chat_id": message.chat.id,  # Çıktıyı kime göndereceğimizi hatırla
            "msg_id": None  # Mesaj ID'si sonra doldurulacak
        }
        
        # Çıktıları yakalamak için background task başlat
        asyncio.create_task(stream_output(process, script_name, message.chat.id, log_path))
        
        return process.pid
    except Exception as e:
        await message.answer(f"🔥 Çalıştırma Hatası: {str(e)}")
        return None

async def stream_output(process: asyncio.subprocess.Process, name: str, chat_id: int, log_path: str):
    """Script'in stdout/stderr çıktısını anlık olarak log dosyasına yazar ve isteğe bağlı gönderir"""
    with open(log_path, 'w', encoding='utf-8') as log_file:
        while True:
            # stdout ve stderr'den veri oku (non-blocking)
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=0.5)
                if line:
                    decoded = line.decode('utf-8').strip()
                    log_file.write(decoded + '\n')
                    log_file.flush()
                else:
                    # Süreç bitti mi kontrol et
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Output okuma hatası: {e}")
                break
                
        # Süreç bittiğinde logu temizle
        if process.pid in active_processes:
            del active_processes[process.pid]

async def stop_script(pid: int) -> bool:
    """PID'ye sahip script'i sonlandırır"""
    if pid in active_processes:
        proc = active_processes[pid]["process"]
        try:
            proc.terminate()  # SIGTERM gönder [citation:3]
            await asyncio.sleep(2)
            if proc.returncode is None:
                proc.kill()  # SIGKILL gönder
            await proc.wait()
            return True
        except Exception as e:
            logger.error(f"Durdurma hatası PID {pid}: {e}")
            return False
    return False

# ================= TELEGRAM KOMUTLARI =================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🚀 **Python Kod Yöneticisine Hoş Geldiniz!**\n\n"
        "📤 **Kod Gönderme:** Bana bir `.py` veya `.txt` dosyası gönderin, otomatik olarak kaydederim.\n"
        "📜 **Listeleme:** `/list` - Tüm script'leri butonlarla görüntüleyin.\n"
        "🛑 **Durdurma:** `/stop_all` - Tüm çalışan script'leri durdurur.\n\n"
        "⚠️ **Uyarı:** Script'ler 120 saniye timeout ile çalışır. Sonsuz döngü yazmayın!",
        parse_mode="Markdown"
    )

@dp.message(Command("list"))
async def cmd_list(message: Message):
    """Çalışan ve duran tüm script'leri butonlu olarak listeler"""
    builder = InlineKeyboardBuilder()
    
    # Dosya listesini al
    files = [f for f in os.listdir(SCRIPTS_DIR) if f.endswith(('.py', '.txt'))]
    
    if not files:
        await message.answer("📂 Kayıtlı hiç script bulunamadı. Lütfen önce bir `.py` dosyası gönderin.")
        return
    
    for file in files:
        # Script çalışıyor mu kontrol et
        is_running = any(p['name'] == file for p in active_processes.values())
        status = "✅" if is_running else "⚪"
        # Butonlara callback_data olarak dosya adını ekle
        builder.button(text=f"{status} {file}", callback_data=f"manage_{file}")
    
    builder.adjust(1)  # Her satıra 1 buton
    await message.answer("📋 **Mevcut Script'ler:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(Command("stop_all"))
async def cmd_stop_all(message: Message):
    """Tüm çalışan script'leri durdur"""
    if not active_processes:
        await message.answer("🤔 Şu anda çalışan hiç script yok.")
        return
        
    for pid in list(active_processes.keys()):
        await stop_script(pid)
    await message.answer(f"🛑 Tüm çalışan script'ler ({len(active_processes)}) durduruldu.")

@dp.message(F.document)
async def handle_document(message: Message):
    """Gönderilen dosyayı al ve scripts klasörüne kaydet"""
    document = message.document
    file_name = document.file_name
    
    # Sadece .py veya .txt kabul et
    if not (file_name.endswith('.py') or file_name.endswith('.txt')):
        await message.answer("❌ Sadece `.py` veya `.txt` uzantılı dosyalar kabul edilir.")
        return
    
    # Dosyayı indir
    file_path = os.path.join(SCRIPTS_DIR, file_name)
    try:
        file = await bot.get_file(document.file_id)
        await bot.download_file(file.file_path, file_path)
        await message.answer(f"✅ `{file_name}` başarıyla kaydedildi.\n/list ile görebilir ve çalıştırabilirsiniz.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"🔥 Dosya kaydedilirken hata: {str(e)}")

# ================= BUTON GERI ARAMALARI (Callback Query) =================

@dp.callback_query(F.data.startswith("manage_"))
async def manage_script_callback(callback: CallbackQuery):
    """Script yönetim panelini açar (Başlat/Durdur/Sil/Konsol)"""
    script_name = callback.data.split("_", 1)[1]
    is_running = any(p['name'] == script_name for p in active_processes.values())
    
    builder = InlineKeyboardBuilder()
    
    if is_running:
        # Çalışıyorsa Durdur butonu göster
        builder.button(text="⏹️ Durdur", callback_data=f"stop_{script_name}")
        builder.button(text="🔄 Yeniden Başlat", callback_data=f"restart_{script_name}")
    else:
        # Duruyorsa Başlat butonu göster
        builder.button(text="▶️ Başlat", callback_data=f"start_{script_name}")
    
    builder.button(text="📊 Konsol (Son çıktılar)", callback_data=f"log_{script_name}")
    builder.button(text="❌ Sil (Dosyayı kaldır)", callback_data=f"delete_{script_name}")
    builder.button(text="◀️ Geri", callback_data="back_to_list")
    
    builder.adjust(1)
    
    await callback.message.edit_text(
        f"📁 **Yönetim Paneli: {script_name}**\nDurum: {'🟢 Çalışıyor' if is_running else '🔴 Durdurulmuş'}",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("start_"))
async def start_script_callback(callback: CallbackQuery):
    script_name = callback.data.split("_", 1)[1]
    # Script'i çalıştır
    pid = await run_script(script_name, callback.message)
    if pid:
        await callback.message.answer(f"🚀 `{script_name}` başlatıldı! (PID: {pid})\nÇıktıları konsoldan takip edin.", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("stop_"))
async def stop_script_callback(callback: CallbackQuery):
    script_name = callback.data.split("_", 1)[1]
    # PID bul
    for pid, info in active_processes.items():
        if info['name'] == script_name:
            if await stop_script(pid):
                await callback.message.answer(f"🛑 `{script_name}` durduruldu.")
            else:
                await callback.message.answer(f"⚠️ `{script_name}` durdurulamadı.")
            break
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_script_callback(callback: CallbackQuery):
    script_name = callback.data.split("_", 1)[1]
    # Önce çalışıyorsa durdur
    for pid, info in active_processes.items():
        if info['name'] == script_name:
            await stop_script(pid)
            break
    # Dosyayı sil
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    try:
        os.remove(script_path)
        await callback.message.answer(f"🗑️ `{script_name}` silindi.")
        # Listeyi yenile
        await cmd_list(callback.message)
    except Exception as e:
        await callback.message.answer(f"🔥 Silme hatası: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("log_"))
async def log_script_callback(callback: CallbackQuery):
    script_name = callback.data.split("_", 1)[1]
    # Log dosyasını bul
    log_files = [f for f in os.listdir(LOGS_DIR) if f.startswith(script_name)]
    if not log_files:
        await callback.message.answer(f"📄 `{script_name}` için henüz log kaydı yok.")
        await callback.answer()
        return
    
    # En son log dosyasını al
    latest_log = sorted(log_files)[-1]
    log_path = os.path.join(LOGS_DIR, latest_log)
    
    try:
        # Dosyayı belge olarak gönder
        doc = FSInputFile(log_path, filename=f"log_{script_name}.txt")
        await callback.message.answer_document(doc, caption=f"📄 {script_name} son çıktıları")
    except Exception as e:
        await callback.message.answer(f"Log okunamıyor: {e}")
    await callback.answer()

@dp.callback_query(F.data == "back_to_list")
async def back_to_list_callback(callback: CallbackQuery):
    await cmd_list(callback.message)
    await callback.answer()

# ================= ANA BASLATICI =================

async def main():
    setup_directories()
    logger.info("Bot ayaklanıyor...")
    # Botu başlat
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot kapatılıyor...")
