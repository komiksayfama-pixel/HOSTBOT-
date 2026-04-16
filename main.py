#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UltimatePyRunner - Telegram'da Python Kodları Çalıştırma Botu
- Çoklu işlem yönetimi
- Admin onay sistemi
- Dosya yükleme/düzenleme
- Butonlu interaktif arayüz
- CPU/RAM optimizasyonu
"""

import asyncio
import os
import sys
import subprocess
import json
import hashlib
import time
import shutil
import psutil
import signal
import zipfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Any
from dataclasses import dataclass, asdict
from enum import Enum
import logging
import traceback

# Telegram kütüphaneleri
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.constants import ParseMode

# ==================== KONFIGÜRASYON ====================

TOKEN = "8422394784:AAEexqJ4P6d5DsfxTdkAhZ2e39y3CPFYhzw"  # @BotFather'dan al
ADMIN_IDS = [8641504826]  # Admin Telegram ID'leri

# Çalışma dizinleri
BASE_DIR = Path("bot_workspace")
CODE_DIR = BASE_DIR / "codes"
LOGS_DIR = BASE_DIR / "logs"
TEMP_DIR = BASE_DIR / "temp"
PROCESS_INFO_FILE = BASE_DIR / "processes.json"

# Limitler
MAX_PROCESSES = 10
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_CONCURRENT_USERS = 5
CPU_LIMIT_PERCENT = 80
RAM_LIMIT_MB = 512

# Zaman aşımları
PROCESS_TIMEOUT = 300  # 5 dakika
CLEANUP_INTERVAL = 60  # 60 saniye

# Logging ayarları
logging.basicConfig(
    format='%(asctime)s - %name)s - %levelname)s - %message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Dizinleri oluştur
for dir_path in [BASE_DIR, CODE_DIR, LOGS_DIR, TEMP_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# ==================== DATA MODELS ====================

class ProcessStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"
    PENDING_APPROVAL = "pending_approval"

@dataclass
class UserProcess:
    """Kullanıcı çalıştırdığı işlem modeli"""
    process_id: str
    user_id: int
    file_name: str
    status: ProcessStatus
    pid: Optional[int]
    start_time: float
    end_time: Optional[float]
    log_file: str
    approval_needed: bool
    approved_by: Optional[int]

@dataclass
class CodeFile:
    """Kullanıcı dosyası modeli"""
    file_id: str
    user_id: int
    file_name: str
    file_path: str
    upload_time: float
    size: int
    hash: str

# ==================== PROCESS MANAGER ====================

class ProcessManager:
    """İşlem yöneticisi - Python kodlarını çalıştırır ve yönetir"""
    
    def __init__(self):
        self.processes: Dict[str, UserProcess] = {}
        self.code_files: Dict[str, CodeFile] = {}
        self.user_sessions: Dict[int, Dict[str, Any]] = {}
        self.load_state()
    
    def load_state(self):
        """Kayıtlı işlemleri yükle"""
        if PROCESS_INFO_FILE.exists():
            try:
                with open(PROCESS_INFO_FILE, 'r') as f:
                    data = json.load(f)
                    for pid_str, proc_data in data.get('processes', {}).items():
                        proc_data['status'] = ProcessStatus(proc_data['status'])
                        self.processes[pid_str] = UserProcess(**proc_data)
                    for fid, file_data in data.get('code_files', {}).items():
                        self.code_files[fid] = CodeFile(**file_data)
            except Exception as e:
                logger.error(f"Durum yüklenirken hata: {e}")
    
    def save_state(self):
        """İşlem durumunu kaydet"""
        data = {
            'processes': {},
            'code_files': {}
        }
        for pid_str, proc in self.processes.items():
            proc_dict = asdict(proc)
            proc_dict['status'] = proc.status.value
            data['processes'][pid_str] = proc_dict
        for fid, file in self.code_files.items():
            data['code_files'][fid] = asdict(file)
        
        with open(PROCESS_INFO_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    
    def generate_process_id(self, user_id: int, file_name: str) -> str:
        """Benzersiz işlem ID'si oluştur"""
        timestamp = int(time.time())
        raw = f"{user_id}_{file_name}_{timestamp}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]
    
    async def run_code(self, file_path: Path, user_id: int, file_name: str, 
                       requires_approval: bool = False) -> Tuple[bool, Optional[str], Optional[str]]:
        """Python kodunu çalıştır ve yönet"""
        
        # İşlem limiti kontrolü
        user_processes = [p for p in self.processes.values() 
                         if p.user_id == user_id and p.status == ProcessStatus.RUNNING]
        if len(user_processes) >= MAX_PROCESSES:
            return False, f"❌ Maksimum {MAX_PROCESSES} aktif işleminiz var!", None
        
        process_id = self.generate_process_id(user_id, file_name)
        log_file = LOGS_DIR / f"{process_id}.log"
        
        # Admin onayı gerekiyorsa bekleme durumu
        if requires_approval and user_id not in ADMIN_IDS:
            process = UserProcess(
                process_id=process_id,
                user_id=user_id,
                file_name=file_name,
                status=ProcessStatus.PENDING_APPROVAL,
                pid=None,
                start_time=time.time(),
                end_time=None,
                log_file=str(log_file),
                approval_needed=True,
                approved_by=None
            )
            self.processes[process_id] = process
            self.save_state()
            
            # Admin'e bildir
            await self.notify_admins(process_id, user_id, file_name)
            return True, f"⏳ Kod admin onayına gönderildi!\n📝 İşlem ID: `{process_id}`", process_id
        
        # Doğrudan çalıştır
        return await self._execute_code(file_path, process_id, user_id, file_name, log_file)
    
    async def _execute_code(self, file_path: Path, process_id: str, user_id: int, 
                            file_name: str, log_file: Path) -> Tuple[bool, str, str]:
        """Kodu çalıştır"""
        try:
            # Önceki işlemleri temizle
            await self.cleanup_zombie_processes()
            
            # Kodu çalıştır
            with open(log_file, 'w') as log_f:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(file_path),
                    stdout=log_f,
                    stderr=log_f,
                    cwd=str(file_path.parent),
                    preexec_fn=os.setsid if os.name != 'nt' else None
                )
            
            # İşlemi kaydet
            user_process = UserProcess(
                process_id=process_id,
                user_id=user_id,
                file_name=file_name,
                status=ProcessStatus.RUNNING,
                pid=process.pid,
                start_time=time.time(),
                end_time=None,
                log_file=str(log_file),
                approval_needed=False,
                approved_by=None
            )
            self.processes[process_id] = user_process
            self.save_state()
            
            # Arkaplanda çıkış kodunu kontrol et
            asyncio.create_task(self._monitor_process(process, process_id))
            
            return True, f"✅ Kod başarıyla çalışıyor!\n📝 İşlem ID: `{process_id}`\n🆔 PID: `{process.pid}`", process_id
            
        except Exception as e:
            logger.error(f"Kod çalıştırma hatası: {e}")
            return False, f"❌ Çalıştırma hatası: {str(e)}", None
    
    async def _monitor_process(self, process: asyncio.subprocess.Process, process_id: str):
        """İşlemi izle ve tamamlanınca güncelle"""
        try:
            return_code = await process.wait()
            
            if process_id in self.processes:
                self.processes[process_id].status = ProcessStatus.COMPLETED if return_code == 0 else ProcessStatus.ERROR
                self.processes[process_id].end_time = time.time()
                self.save_state()
                
                # Kullanıcıya bildir
                await self.notify_user_completion(process_id, return_code)
                
        except Exception as e:
            logger.error(f"İşlem izleme hatası {process_id}: {e}")
    
    async def stop_process(self, process_id: str, user_id: int) -> Tuple[bool, str]:
        """Çalışan işlemi durdur"""
        if process_id not in self.processes:
            return False, "❌ İşlem bulunamadı!"
        
        process = self.processes[process_id]
        if process.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Bu işlemi durdurma yetkiniz yok!"
        
        if process.status != ProcessStatus.RUNNING:
            return False, f"❌ İşlem {process.status.value} durumunda!"
        
        try:
            if process.pid:
                if os.name == 'nt':  # Windows
                    subprocess.run(['taskkill', '/F', '/PID', str(process.pid)], capture_output=True)
                else:  # Linux/Mac
                    os.kill(process.pid, signal.SIGTERM)
            
            process.status = ProcessStatus.STOPPED
            process.end_time = time.time()
            self.save_state()
            return True, f"✅ İşlem durduruldu: `{process_id}`"
            
        except Exception as e:
            logger.error(f"İşlem durdurma hatası: {e}")
            return False, f"❌ Durdurma hatası: {str(e)}"
    
    async def restart_process(self, process_id: str, user_id: int) -> Tuple[bool, str]:
        """İşlemi yeniden başlat"""
        if process_id not in self.processes:
            return False, "❌ İşlem bulunamadı!"
        
        old_process = self.processes[process_id]
        if old_process.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!"
        
        # Eski işlemi durdur
        await self.stop_process(process_id, user_id)
        
        # Dosyayı bul
        file_path = CODE_DIR / old_process.file_name
        if not file_path.exists():
            return False, f"❌ Dosya bulunamadı: {old_process.file_name}"
        
        # Yeniden başlat
        success, msg, new_id = await self.run_code(file_path, user_id, old_process.file_name, False)
        return success, f"🔄 İşlem yeniden başlatıldı!\n{msg}"
    
    async def get_process_log(self, process_id: str, user_id: int, lines: int = 100) -> Tuple[bool, str]:
        """İşlem loglarını al"""
        if process_id not in self.processes:
            return False, "❌ İşlem bulunamadı!"
        
        process = self.processes[process_id]
        if process.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!"
        
        log_path = Path(process.log_file)
        if not log_path.exists():
            return False, "❌ Log dosyası bulunamadı!"
        
        try:
            with open(log_path, 'r') as f:
                all_lines = f.readlines()
                last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                
            log_text = ''.join(last_lines)
            if len(log_text) > 4000:
                log_text = log_text[-4000:] + "\n\n... (kesildi)"
            
            return True, f"📝 Loglar ({process.file_name}):\n```\n{log_text}\n```"
        except Exception as e:
            return False, f"❌ Log okuma hatası: {str(e)}"
    
    async def cleanup_zombie_processes(self):
        """Zombi prosesleri temizle"""
        current_time = time.time()
        to_remove = []
        
        for pid_str, process in self.processes.items():
            # 1 saatten eski tamamlanmış işlemleri temizle
            if process.status in [ProcessStatus.COMPLETED, ProcessStatus.ERROR, ProcessStatus.STOPPED]:
                if process.end_time and (current_time - process.end_time) > 3600:
                    to_remove.append(pid_str)
        
        for pid_str in to_remove:
            del self.processes[pid_str]
        
        if to_remove:
            self.save_state()
            logger.info(f"{len(to_remove)} eski işlem temizlendi")
    
    async def notify_admins(self, process_id: str, user_id: int, file_name: str):
        """Adminlere onay bildirimi gönder"""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Onayla", callback_data=f"approve_{process_id}"),
            InlineKeyboardButton("❌ Reddet", callback_data=f"reject_{process_id}")
        ], [
            InlineKeyboardButton("📝 Logları Gör", callback_data=f"preview_{process_id}")
        ]])
        
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_message(
                    chat_id=admin_id,
                    text=f"🔔 **Yeni Kod Onay Bekliyor!**\n\n"
                         f"👤 Kullanıcı: `{user_id}`\n"
                         f"📁 Dosya: `{file_name}`\n"
                         f"🆔 İşlem ID: `{process_id}`",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Admin bildirim hatası {admin_id}: {e}")
    
    async def approve_process(self, process_id: str, admin_id: int) -> Tuple[bool, str]:
        """Admin onayı ile işlemi başlat"""
        if process_id not in self.processes:
            return False, "❌ İşlem bulunamadı!"
        
        process = self.processes[process_id]
        if process.status != ProcessStatus.PENDING_APPROVAL:
            return False, f"❌ İşlem zaten {process.status.value} durumunda!"
        
        # Dosyayı bul
        file_path = CODE_DIR / process.file_name
        if not file_path.exists():
            return False, f"❌ Dosya bulunamadı: {process.file_name}"
        
        # Çalıştır
        success, msg, new_id = await self._execute_code(
            file_path, process_id, process.user_id, 
            process.file_name, Path(process.log_file)
        )
        
        if success:
            self.processes[process_id].approved_by = admin_id
            self.processes[process_id].status = ProcessStatus.RUNNING
            self.processes[process_id].approval_needed = False
            self.save_state()
            
            # Kullanıcıya bildir
            await application.bot.send_message(
                chat_id=process.user_id,
                text=f"✅ **Kodunuz Onaylandı!**\n\n"
                     f"📝 İşlem ID: `{process_id}`\n"
                     f"👤 Onaylayan Admin: `{admin_id}`\n\n"
                     f"Kod başarıyla çalışmaya başladı.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        return success, msg
    
    async def notify_user_completion(self, process_id: str, return_code: int):
        """Kullanıcıya işlem tamamlandı bildirimi gönder"""
        if process_id in self.processes:
            process = self.processes[process_id]
            status_text = "✅ Tamamlandı" if return_code == 0 else "❌ Hata ile Tamamlandı"
            
            await application.bot.send_message(
                chat_id=process.user_id,
                text=f"🔔 **İşlem {status_text}**\n\n"
                     f"📁 Dosya: `{process.file_name}`\n"
                     f"🆔 İşlem ID: `{process_id}`\n"
                     f"⏱️ Süre: {int(time.time() - process.start_time)} saniye\n\n"
                     f"Logları görmek için `/log {process_id}` komutunu kullanın.",
                parse_mode=ParseMode.MARKDOWN
            )
    
    def get_status_text(self) -> str:
        """Sistem durumu metni"""
        running = sum(1 for p in self.processes.values() if p.status == ProcessStatus.RUNNING)
        pending = sum(1 for p in self.processes.values() if p.status == ProcessStatus.PENDING_APPROVAL)
        completed = sum(1 for p in self.processes.values() if p.status == ProcessStatus.COMPLETED)
        total = len(self.processes)
        
        # Sistem kaynakları
        cpu_percent = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory()
        
        return f"""📊 **Sistem Durumu**

📦 **İşlemler:**
• Aktif: `{running}`
• Bekleyen Onay: `{pending}`
• Tamamlanan: `{completed}`
• Toplam: `{total}`

💻 **Sistem Kaynakları:**
• CPU: `{cpu_percent}%`
• RAM: `{memory.percent}%`
• Boş RAM: `{memory.available // (1024**2)}MB`

⚙️ **Limitler:**
• Maks. İşlem/Kullanıcı: `{MAX_PROCESSES}`
• CPU Limiti: `{CPU_LIMIT_PERCENT}%`
• RAM Limiti: `{RAM_LIMIT_MB}MB`"""

# ==================== FILE MANAGER ====================

class FileManager:
    """Dosya yönetimi - yükleme, silme, listeleme, düzenleme"""
    
    def __init__(self, process_manager: ProcessManager):
        self.pm = process_manager
    
    async def save_code_file(self, file_data: bytes, file_name: str, user_id: int) -> Tuple[bool, str, Optional[str]]:
        """Kod dosyasını kaydet"""
        if len(file_data) > MAX_FILE_SIZE:
            return False, f"❌ Dosya çok büyük! Maksimum {MAX_FILE_SIZE // (1024**2)}MB", None
        
        if not file_name.endswith('.py'):
            file_name += '.py'
        
        # Güvenli dosya adı
        safe_name = f"{user_id}_{int(time.time())}_{file_name}"
        file_path = CODE_DIR / safe_name
        
        try:
            with open(file_path, 'wb') as f:
                f.write(file_data)
            
            file_hash = hashlib.md5(file_data).hexdigest()
            file_id = hashlib.md5(safe_name.encode()).hexdigest()[:12]
            
            code_file = CodeFile(
                file_id=file_id,
                user_id=user_id,
                file_name=safe_name,
                file_path=str(file_path),
                upload_time=time.time(),
                size=len(file_data),
                hash=file_hash
            )
            self.pm.code_files[file_id] = code_file
            self.pm.save_state()
            
            return True, f"✅ Dosya kaydedildi: `{safe_name}`", file_id
            
        except Exception as e:
            return False, f"❌ Kayıt hatası: {str(e)}", None
    
    async def list_user_files(self, user_id: int) -> List[CodeFile]:
        """Kullanıcının dosyalarını listele"""
        return [f for f in self.pm.code_files.values() if f.user_id == user_id]
    
    async def delete_file(self, file_id: str, user_id: int) -> Tuple[bool, str]:
        """Dosyayı sil"""
        if file_id not in self.pm.code_files:
            return False, "❌ Dosya bulunamadı!"
        
        file = self.pm.code_files[file_id]
        if file.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!"
        
        try:
            Path(file.file_path).unlink(missing_ok=True)
            del self.pm.code_files[file_id]
            self.pm.save_state()
            return True, f"✅ Dosya silindi: `{file.file_name}`"
        except Exception as e:
            return False, f"❌ Silme hatası: {str(e)}"
    
    async def edit_file(self, file_id: str, user_id: int, new_content: str) -> Tuple[bool, str]:
        """Dosyayı düzenle"""
        if file_id not in self.pm.code_files:
            return False, "❌ Dosya bulunamadı!"
        
        file = self.pm.code_files[file_id]
        if file.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!"
        
        try:
            with open(file.file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            # Hash'i güncelle
            with open(file.file_path, 'rb') as f:
                file.hash = hashlib.md5(f.read()).hexdigest()
            
            self.pm.save_state()
            return True, f"✅ Dosya düzenlendi: `{file.file_name}`"
        except Exception as e:
            return False, f"❌ Düzenleme hatası: {str(e)}"
    
    async def get_file_content(self, file_id: str, user_id: int) -> Tuple[bool, str, Optional[str]]:
        """Dosya içeriğini al"""
        if file_id not in self.pm.code_files:
            return False, "❌ Dosya bulunamadı!", None
        
        file = self.pm.code_files[file_id]
        if file.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!", None
        
        try:
            with open(file.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return True, "", content
        except Exception as e:
            return False, f"❌ Okuma hatası: {str(e)}", None
    
    async def upload_zip_archive(self, zip_data: bytes, user_id: int) -> Tuple[bool, str, List[str]]:
        """ZIP arşivinden birden çok dosya yükle"""
        extracted_files = []
        
        try:
            with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp_file:
                tmp_file.write(zip_data)
                tmp_path = tmp_file.name
            
            extract_dir = TEMP_DIR / f"extract_{user_id}_{int(time.time())}"
            extract_dir.mkdir(exist_ok=True)
            
            with zipfile.ZipFile(tmp_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            # .py dosyalarını bul ve kaydet
            for py_file in extract_dir.rglob('*.py'):
                with open(py_file, 'rb') as f:
                    content = f.read()
                success, msg, file_id = await self.save_code_file(content, py_file.name, user_id)
                if success:
                    extracted_files.append(py_file.name)
            
            # Temizlik
            os.unlink(tmp_path)
            shutil.rmtree(extract_dir)
            
            if extracted_files:
                return True, f"✅ {len(extracted_files)} dosya yüklendi!", extracted_files
            else:
                return False, "❌ ZIP içinde .py dosyası bulunamadı!", []
                
        except Exception as e:
            return False, f"❌ ZIP açma hatası: {str(e)}", []

# ==================== TELEGRAM BOT HANDLERS ====================

# Global instance'lar
pm = ProcessManager()
fm = FileManager(pm)

# Helper: Admin kontrolü
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Helper: Menü oluşturma
def get_main_menu(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🚀 Kod Çalıştır", callback_data="run_code")],
        [InlineKeyboardButton("📁 Dosyalarım", callback_data="my_files")],
        [InlineKeyboardButton("📊 Aktif İşlemler", callback_data="active_processes")],
        [InlineKeyboardButton("📝 Log Göster", callback_data="show_logs")],
        [InlineKeyboardButton("🔄 İşlem Yeniden Başlat", callback_data="restart_process")],
        [InlineKeyboardButton("📦 ZIP Yükle", callback_data="upload_zip")],
        [InlineKeyboardButton("📈 Sistem Durumu", callback_data="system_status")]
    ]
    
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
        keyboard.append([InlineKeyboardButton("⏳ Bekleyen Onaylar", callback_data="pending_approvals")])
    
    keyboard.append([InlineKeyboardButton("❓ Yardım", callback_data="help")])
    
    return InlineKeyboardMarkup(keyboard)

# Komutlar
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start - Başlangıç mesajı"""
    user = update.effective_user
    welcome_text = f"""🎉 **Hoş Geldin, {user.first_name}!**

Ben **UltimatePyRunner** - Güçlü Python Kod Hosting Botu!

🚀 **Özelliklerim:**
• Python kodlarını çalıştırma
• Admin onaylı çalıştırma
• İşlem durdurma/yeniden başlatma
• Dosya yükleme/düzenleme/silme
• ZIP arşivinden toplu yükleme
• Detaylı log görüntüleme
• Sistem kaynak izleme

📝 **Nasıl Kullanılır:**
Aşağıdaki butonları kullanarak işlemlerini yönetebilirsin!

💡 **İpucu:** Admin onayı gereken kodlar adminler tarafından incelenip onaylandıktan sonra çalışır."""
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_menu(is_admin(user.id)),
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dosya yükleme işlemi"""
    user = update.effective_user
    document = update.message.document
    
    if not document:
        await update.message.reply_text("❌ Lütfen bir dosya gönder!")
        return
    
    # İndir
    file = await context.bot.get_file(document.file_id)
    file_data = await file.download_as_bytearray()
    
    # Onay gerekiyor mu?
    requires_approval = "approve" in context.user_data.get("mode", "")
    
    success, msg, file_id = await fm.save_code_file(bytes(file_data), document.file_name, user.id)
    
    if success:
        # Kodu çalıştır
        file_path = CODE_DIR / file_id
        run_success, run_msg, process_id = await pm.run_code(file_path, user.id, document.file_name, requires_approval)
        await update.message.reply_text(f"{msg}\n\n{run_msg}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Metin komutları"""
    user = update.effective_user
    text = update.message.text.strip()
    
    if text.startswith('/run '):
        # /run komutu ile kod çalıştırma
        pass  # Basitlik için eklenecek
    
    elif text.startswith('/log '):
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Kullanım: `/log <process_id>`", parse_mode=ParseMode.MARKDOWN)
            return
        
        process_id = parts[1]
        success, log_text = await pm.get_process_log(process_id, user.id)
        await update.message.reply_text(log_text, parse_mode=ParseMode.MARKDOWN)
    
    elif text.startswith('/stop '):
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Kullanım: `/stop <process_id>`", parse_mode=ParseMode.MARKDOWN)
            return
        
        process_id = parts[1]
        success, msg = await pm.stop_process(process_id, user.id)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buton callback işlemleri"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    data = query.data
    
    # Admin onay işlemleri
    if data.startswith('approve_'):
        process_id = data.replace('approve_', '')
        success, msg = await pm.approve_process(process_id, user.id)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith('reject_'):
        process_id = data.replace('reject_', '')
        if process_id in pm.processes:
            proc = pm.processes[process_id]
            proc.status = ProcessStatus.ERROR
            pm.save_state()
            await query.edit_message_text(f"❌ İşlem reddedildi: {process_id}")
            
            # Kullanıcıya bildir
            await application.bot.send_message(
                chat_id=proc.user_id,
                text=f"❌ Kodunuz admin tarafından reddedildi.\nİşlem ID: `{process_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif data == "run_code":
        context.user_data["mode"] = "run"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Admin Onaylı Çalıştır", callback_data="run_with_approve")],
            [InlineKeyboardButton("⚡ Doğrudan Çalıştır", callback_data="run_direct")],
            [InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")]
        ])
        await query.edit_message_text(
            "🚀 **Kod Çalıştırma Seçenekleri**\n\n"
            "• **Admin Onaylı:** Kod admin tarafından incelenip onaylandıktan sonra çalışır\n"
            "• **Doğrudan Çalıştır:** Kod hemen çalıştırılır (güvendiğiniz kodlar için)",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "run_with_approve":
        context.user_data["mode"] = "approve"
        await query.edit_message_text(
            "✅ **Admin Onaylı Mod Seçildi!**\n\n"
            "Şimdi çalıştırmak istediğin Python (.py) dosyasını gönder.\n"
            "Dosya admin onayına gidecek ve onaylandıktan sonra çalışacak.\n\n"
            "İptal etmek için /cancel yazabilirsin.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "run_direct":
        context.user_data["mode"] = "direct"
        await query.edit_message_text(
            "⚡ **Doğrudan Çalıştırma Modu Seçildi!**\n\n"
            "Şimdi çalıştırmak istediğin Python (.py) dosyasını gönder.\n"
            "Kod hemen çalışmaya başlayacak.\n\n"
            "İptal etmek için /cancel yazabilirsin.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "my_files":
        files = await fm.list_user_files(user.id)
        if not files:
            await query.edit_message_text("📁 Henüz hiç dosyanız yok!\n\nDosya yüklemek için /start menüsünü kullanabilirsiniz.")
            return
        
        text = "📁 **Dosyalarınız:**\n\n"
        keyboard = []
        for f in files:
            text += f"• `{f.file_name}` ({(f.size // 1024)}KB)\n"
            keyboard.append([InlineKeyboardButton(
                f"📄 {f.file_name[:20]}", 
                callback_data=f"file_{f.file_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")])
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("file_"):
        file_id = data.replace("file_", "")
        file = pm.code_files.get(file_id)
        if not file:
            await query.edit_message_text("❌ Dosya bulunamadı!")
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Düzenle", callback_data=f"edit_{file_id}")],
            [InlineKeyboardButton("🗑️ Sil", callback_data=f"delete_{file_id}")],
            [InlineKeyboardButton("🚀 Çalıştır", callback_data=f"execute_{file_id}")],
            [InlineKeyboardButton("📄 İçeriği Gör", callback_data=f"view_{file_id}")],
            [InlineKeyboardButton("◀️ Geri", callback_data="my_files")]
        ])
        
        await query.edit_message_text(
            f"📄 **Dosya:** `{file.file_name}`\n"
            f"📏 Boyut: {file.size // 1024}KB\n"
            f"🕐 Yüklenme: {datetime.fromtimestamp(file.upload_time).strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Ne yapmak istiyorsun?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("delete_"):
        file_id = data.replace("delete_", "")
        success, msg = await fm.delete_file(file_id, user.id)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("view_"):
        file_id = data.replace("view_", "")
        success, error, content = await fm.get_file_content(file_id, user.id)
        if success and content:
            if len(content) > 3000:
                content = content[:3000] + "\n\n... (kesildi)"
            await query.edit_message_text(
                f"📄 **Dosya İçeriği:**\n```python\n{content}\n```",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(error, parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("edit_"):
        file_id = data.replace("edit_", "")
        context.user_data["editing_file"] = file_id
        await query.edit_message_text(
            "✏️ **Dosya Düzenleme Modu**\n\n"
            "Yeni kod içeriğini gönder.\n"
            "İşlemi iptal etmek için /cancel yaz.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("execute_"):
        file_id = data.replace("execute_", "")
        file = pm.code_files.get(file_id)
        if file:
            file_path = Path(file.file_path)
            success, msg, process_id = await pm.run_code(file_path, user.id, file.file_name, False)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "active_processes":
        user_processes = [p for p in pm.processes.values() if p.user_id == user.id]
        if not user_processes:
            await query.edit_message_text("📊 Aktif işleminiz bulunmuyor.")
            return
        
        text = "📊 **İşlemleriniz:**\n\n"
        keyboard = []
        for p in user_processes:
            status_emoji = {
                ProcessStatus.RUNNING: "🟢",
                ProcessStatus.STOPPED: "🔴",
                ProcessStatus.COMPLETED: "✅",
                ProcessStatus.ERROR: "❌",
                ProcessStatus.PENDING_APPROVAL: "⏳"
            }.get(p.status, "⚪")
            
            text += f"{status_emoji} **{p.file_name}**\n"
            text += f"   ID: `{p.process_id}`\n"
            text += f"   Durum: {p.status.value}\n"
            text += f"   Başlangıç: {datetime.fromtimestamp(p.start_time).strftime('%H:%M:%S')}\n\n"
            
            if p.status == ProcessStatus.RUNNING:
                keyboard.append([InlineKeyboardButton(
                    f"🛑 Durdur: {p.file_name[:15]}", 
                    callback_data=f"stop_{p.process_id}"
                )])
        
        keyboard.append([InlineKeyboardButton("🔄 Yenile", callback_data="active_processes")])
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")])
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("stop_"):
        process_id = data.replace("stop_", "")
        success, msg = await pm.stop_process(process_id, user.id)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "show_logs":
        user_processes = [p for p in pm.processes.values() if p.user_id == user.id]
        if not user_processes:
            await query.edit_message_text("📝 Hiç işlem kaydınız yok.")
            return
        
        keyboard = []
        for p in user_processes[-10:]:
            keyboard.append([InlineKeyboardButton(
                f"📝 {p.file_name[:20]}", 
                callback_data=f"log_{p.process_id}"
            )])
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")])
        
        await query.edit_message_text(
            "📝 **Son 10 İşleminizin Logları:**\nSeçmek için tıklayın:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("log_"):
        process_id = data.replace("log_", "")
        success, log_text = await pm.get_process_log(process_id, user.id)
        await query.edit_message_text(log_text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "restart_process":
        user_processes = [p for p in pm.processes.values() if p.user_id == user.id]
        running = [p for p in user_processes if p.status == ProcessStatus.RUNNING]
        
        if not running:
            await query.edit_message_text("🔄 Yeniden başlatılacak işlem bulunmuyor.")
            return
        
        keyboard = []
        for p in running:
            keyboard.append([InlineKeyboardButton(
                f"🔄 {p.file_name[:20]}", 
                callback_data=f"restart_{p.process_id}"
            )])
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")])
        
        await query.edit_message_text(
            "🔄 **Yeniden Başlatılacak İşlemi Seç:**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("restart_"):
        process_id = data.replace("restart_", "")
        success, msg = await pm.restart_process(process_id, user.id)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "upload_zip":
        await query.edit_message_text(
            "📦 **ZIP Arşivi Yükleme**\n\n"
            "İçinde .py dosyaları olan bir ZIP arşivi gönder.\n"
            "Arşivdeki tüm Python dosyaları otomatik olarak yüklenecek ve çalıştırılabilir.\n\n"
            "İptal etmek için /cancel",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["mode"] = "zip_upload"
    
    elif data == "system_status":
        status_text = pm.get_status_text()
        await query.edit_message_text(status_text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "admin_panel":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Bu alan sadece adminler içindir!")
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏳ Bekleyen Onaylar", callback_data="pending_approvals")],
            [InlineKeyboardButton("📊 Tüm İşlemler", callback_data="all_processes")],
            [InlineKeyboardButton("👥 Kullanıcı Listesi", callback_data="user_list")],
            [InlineKeyboardButton("⚙️ Sistem Ayarları", callback_data="system_settings")],
            [InlineKeyboardButton("📈 Detaylı Sistem Durumu", callback_data="detailed_status")],
            [InlineKeyboardButton("🗑️ Temizlik Yap", callback_data="cleanup_system")],
            [InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")]
        ])
        
        await query.edit_message_text(
            "👑 **Admin Kontrol Paneli**\n\n"
            "Tüm sistem işlemlerini buradan yönetebilirsin.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "pending_approvals":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz erişim!")
            return
        
        pending = [p for p in pm.processes.values() if p.status == ProcessStatus.PENDING_APPROVAL]
        
        if not pending:
            await query.edit_message_text("⏳ Bekleyen onay talebi yok.")
            return
        
        text = "⏳ **Bekleyen Onaylar:**\n\n"
        keyboard = []
        for p in pending:
            text += f"• **{p.file_name}**\n"
            text += f"  Kullanıcı: `{p.user_id}`\n"
            text += f"  İşlem ID: `{p.process_id}`\n\n"
            keyboard.append([InlineKeyboardButton(
                f"✅ Onayla: {p.file_name[:20]}", 
                callback_data=f"approve_{p.process_id}"
            )])
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "all_processes":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz erişim!")
            return
        
        if not pm.processes:
            await query.edit_message_text("📊 Hiç işlem kaydı yok.")
            return
        
        text = "📊 **Tüm İşlemler:**\n\n"
        for p in list(pm.processes.values())[-20:]:
            text += f"• `{p.file_name}`\n"
            text += f"  Kullanıcı: `{p.user_id}` | Durum: {p.status.value}\n"
            text += f"  ID: `{p.process_id}`\n\n"
        
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "user_list":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz erişim!")
            return
        
        users = set(p.user_id for p in pm.processes.values())
        text = "👥 **Kullanıcı Listesi:**\n\n"
        for uid in users:
            user_processes = [p for p in pm.processes.values() if p.user_id == uid]
            text += f"• `{uid}` - {len(user_processes)} işlem\n"
        
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "detailed_status":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz erişim!")
            return
        
        cpu_percent = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        text = f"""📈 **Detaylı Sistem Durumu**

💻 **CPU:**
• Kullanım: `{cpu_percent}%`
• Çekirdek: `{psutil.cpu_count()}`

🧠 **RAM:**
• Toplam: `{memory.total // (1024**3)}GB`
• Kullanım: `{memory.percent}%`
• Boş: `{memory.available // (1024**2)}MB`

💾 **Disk:**
• Toplam: `{disk.total // (1024**3)}GB`
• Kullanım: `{disk.percent}%`
• Boş: `{disk.free // (1024**3)}GB`

📦 **Bot İstatistikleri:**
• Toplam İşlem: `{len(pm.processes)}`
• Kayıtlı Dosya: `{len(pm.code_files)}`
• Çalışma Dizini: `{BASE_DIR.absolute()}`"""
        
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "cleanup_system":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz erişim!")
            return
        
        await pm.cleanup_zombie_processes()
        
        # Geçici dosyaları temizle
        temp_count = 0
        for f in TEMP_DIR.iterdir():
            if f.is_file():
                f.unlink()
                temp_count += 1
        
        await query.edit_message_text(
            f"🧹 **Temizlik Tamamlandı!**\n\n"
            f"• {len([p for p in pm.processes.values() if p.status in [ProcessStatus.COMPLETED, ProcessStatus.ERROR, ProcessStatus.STOPPED]])} eski işlem kaydı temizlendi\n"
            f"• {temp_count} geçici dosya silindi",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "system_settings":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz erişim!")
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Limitleri Göster", callback_data="show_limits")],
            [InlineKeyboardButton("🔄 Sistem Logları", callback_data="system_logs")],
            [InlineKeyboardButton("◀️ Geri", callback_data="admin_panel")]
        ])
        
        await query.edit_message_text(
            "⚙️ **Sistem Ayarları**\n\n"
            "Mevcut limitler ve konfigürasyon:\n"
            f"• Maksimum İşlem/Kullanıcı: {MAX_PROCESSES}\n"
            f"• Maksimum Dosya Boyutu: {MAX_FILE_SIZE // (1024**2)}MB\n"
            f"• CPU Limiti: {CPU_LIMIT_PERCENT}%\n"
            f"• RAM Limiti: {RAM_LIMIT_MB}MB\n"
            f"• İşlem Zaman Aşımı: {PROCESS_TIMEOUT} saniye",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "back_to_menu":
        await query.edit_message_text(
            "🏠 **Ana Menü**",
            reply_markup=get_main_menu(is_admin(user.id)),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "help":
        help_text = """❓ **Yardım Menüsü**

📌 **Temel Komutlar:**
• `/start` - Botu başlat ve menüyü göster
• `/cancel` - Mevcut işlemi iptal et
• `/log <id>` - İşlem loglarını göster
• `/stop <id>` - Çalışan işlemi durdur

📌 **Özellikler:**

🚀 **Kod Çalıştırma**
• Admin Onaylı: Kod incelenip onaylandıktan sonra çalışır
• Doğrudan Çalıştır: Kod hemen çalıştırılır

📁 **Dosya Yönetimi**
• Dosya yükleme, silme, düzenleme
• ZIP arşivinden toplu yükleme
• Dosya içeriğini görüntüleme

📊 **İşlem Yönetimi**
• Aktif işlemleri listeleme
• İşlem durdurma/yeniden başlatma
• Detaylı log görüntüleme

💡 **İpucu:** Tüm işlemler butonlarla yapılabilir!
Menüden istediğin özelliği seç."""
        
        await query.edit_message_text(
            help_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Geri", callback_data="back_to_menu")]]),
            parse_mode=ParseMode.MARKDOWN
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """İşlem iptal"""
    if "mode" in context.user_data:
        del context.user_data["mode"]
    if "editing_file" in context.user_data:
        del context.user_data["editing_file"]
    
    await update.message.reply_text("❌ Mevcut işlem iptal edildi!")

async def handle_edit_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dosya düzenleme işlemi"""
    if "editing_file" not in context.user_data:
        return
    
    file_id = context.user_data["editing_file"]
    new_content = update.message.text
    
    success, msg = await fm.edit_file(file_id, update.effective_user.id, new_content)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    del context.user_data["editing_file"]

async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ZIP dosyası yükleme"""
    document = update.message.document
    if not document or not document.file_name.endswith('.zip'):
        await update.message.reply_text("❌ Lütfen geçerli bir ZIP dosyası gönder!")
        return
    
    file = await context.bot.get_file(document.file_id)
    zip_data = await file.download_as_bytearray()
    
    success, msg, files = await fm.upload_zip_archive(bytes(zip_data), update.effective_user.id)
    
    if success:
        await update.message.reply_text(
            f"{msg}\n\nYüklenen dosyalar:\n" + "\n".join(f"• `{f}`" for f in files[:10]),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    if "mode" in context.user_data:
        del context.user_data["mode"]

# ==================== ANA ÇALIŞTIRICI ====================

async def main():
    """Botu başlat"""
    global application
    
    # Application oluştur
    application = Application.builder().token(TOKEN).build()
    
    # Komutları ekle
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # Dosya handler'ları
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_file))
    
    # ZIP yükleme handler'ı (önce ZIP kontrolü)
    application.add_handler(MessageHandler(
        filters.Document.FileExtension("zip"), 
        handle_zip_upload
    ))
    
    # Buton callback'leri
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Metin komutları
    application.add_handler(MessageHandler(filters.TEXT & filters.COMMAND, handle_text_command))
    
    # Başlat
    logger.info("Bot başlatılıyor...")
    await application.initialize()
    await application.start()
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot durduruldu.")
    except Exception as e:
        logger.error(f"Kritik hata: {e}")
        traceback.print_exc()
