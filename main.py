#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UltimatePyRunner - Telegram Python Hosting Bot
Render/Heroku/Docker uyumlu - Event loop hatasız
"""

import os
import sys
import subprocess
import json
import hashlib
import time
import shutil
import tempfile
import zipfile
import signal
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple, List
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

try:
    import psutil
except ImportError:
    psutil = None

# ==================== KONFIGÜRASYON ====================

TOKEN = "8422394784:AAEexqJ4P6d5DsfxTdkAhZ2e39y3CPFYhzw"
ADMIN_IDS = [8641504826]

# Çalışma dizinleri
BASE_DIR = Path("bot_workspace")
CODE_DIR = BASE_DIR / "codes"
LOGS_DIR = BASE_DIR / "logs"
TEMP_DIR = BASE_DIR / "temp"
PROCESS_INFO_FILE = BASE_DIR / "processes.json"

# Limitler
MAX_PROCESSES = 5
MAX_FILE_SIZE = 10 * 1024 * 1024
CPU_LIMIT_PERCENT = 80
RAM_LIMIT_MB = 512

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
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
    file_id: str
    user_id: int
    file_name: str
    file_path: str
    upload_time: float
    size: int
    hash: str

# ==================== PROCESS MANAGER ====================

class ProcessManager:
    def __init__(self):
        self.processes: Dict[str, UserProcess] = {}
        self.code_files: Dict[str, CodeFile] = {}
        self.subprocesses: Dict[str, asyncio.subprocess.Process] = {}
        self.load_state()
    
    def load_state(self):
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
        try:
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
        except Exception as e:
            logger.error(f"Kayıt hatası: {e}")
    
    def generate_process_id(self, user_id: int, file_name: str) -> str:
        timestamp = int(time.time())
        raw = f"{user_id}_{file_name}_{timestamp}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]
    
    async def run_code(self, file_path: Path, user_id: int, file_name: str, 
                       requires_approval: bool = False, bot=None) -> Tuple[bool, str, Optional[str]]:
        
        user_processes = [p for p in self.processes.values() 
                         if p.user_id == user_id and p.status == ProcessStatus.RUNNING]
        if len(user_processes) >= MAX_PROCESSES:
            return False, f"❌ Maksimum {MAX_PROCESSES} aktif işleminiz var!", None
        
        process_id = self.generate_process_id(user_id, file_name)
        log_file = LOGS_DIR / f"{process_id}.log"
        
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
            
            if bot:
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(
                            chat_id=admin_id,
                            text=f"🔔 **Yeni Kod Onay Bekliyor!**\n\n👤 Kullanıcı: `{user_id}`\n📁 Dosya: `{file_name}`\n🆔 ID: `{process_id}`",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception as e:
                        logger.error(f"Admin bildirim hatası: {e}")
            
            return True, f"⏳ Kod admin onayına gönderildi!\n📝 İşlem ID: `{process_id}`", process_id
        
        return await self._execute_code(file_path, process_id, user_id, file_name, log_file)
    
    async def _execute_code(self, file_path: Path, process_id: str, user_id: int, 
                            file_name: str, log_file: Path) -> Tuple[bool, str, str]:
        try:
            with open(log_file, 'w') as log_f:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(file_path),
                    stdout=log_f,
                    stderr=log_f,
                    cwd=str(file_path.parent)
                )
            
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
            self.subprocesses[process_id] = process
            self.save_state()
            
            asyncio.create_task(self._monitor_process(process, process_id))
            
            return True, f"✅ Kod çalışıyor!\n📝 ID: `{process_id}`\n🆔 PID: `{process.pid}`", process_id
            
        except Exception as e:
            logger.error(f"Çalıştırma hatası: {e}")
            return False, f"❌ Hata: {str(e)}", None
    
    async def _monitor_process(self, process: asyncio.subprocess.Process, process_id: str):
        try:
            return_code = await process.wait()
            
            if process_id in self.processes:
                self.processes[process_id].status = ProcessStatus.COMPLETED if return_code == 0 else ProcessStatus.ERROR
                self.processes[process_id].end_time = time.time()
                self.save_state()
                
            if process_id in self.subprocesses:
                del self.subprocesses[process_id]
                
        except Exception as e:
            logger.error(f"İzleme hatası {process_id}: {e}")
    
    async def stop_process(self, process_id: str, user_id: int) -> Tuple[bool, str]:
        if process_id not in self.processes:
            return False, "❌ İşlem bulunamadı!"
        
        process = self.processes[process_id]
        if process.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!"
        
        if process.status != ProcessStatus.RUNNING:
            return False, f"❌ İşlem {process.status.value} durumunda!"
        
        try:
            if process_id in self.subprocesses:
                proc = self.subprocesses[process_id]
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
            
            process.status = ProcessStatus.STOPPED
            process.end_time = time.time()
            self.save_state()
            return True, f"✅ İşlem durduruldu: `{process_id}`"
            
        except Exception as e:
            return False, f"❌ Durdurma hatası: {str(e)}"
    
    async def get_process_log(self, process_id: str, user_id: int, lines: int = 100) -> Tuple[bool, str]:
        if process_id not in self.processes:
            return False, "❌ İşlem bulunamadı!"
        
        process = self.processes[process_id]
        if process.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!"
        
        log_path = Path(process.log_file)
        if not log_path.exists():
            return False, "❌ Log dosyası bulunamadı!"
        
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                all_lines = f.readlines()
                last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                
            log_text = ''.join(last_lines)
            if len(log_text) > 4000:
                log_text = log_text[-4000:] + "\n\n... (kesildi)"
            
            return True, f"📝 **{process.file_name}** logları:\n```\n{log_text}\n```"
        except Exception as e:
            return False, f"❌ Log okuma hatası: {str(e)}"
    
    def get_status_text(self) -> str:
        running = sum(1 for p in self.processes.values() if p.status == ProcessStatus.RUNNING)
        pending = sum(1 for p in self.processes.values() if p.status == ProcessStatus.PENDING_APPROVAL)
        completed = sum(1 for p in self.processes.values() if p.status == ProcessStatus.COMPLETED)
        total = len(self.processes)
        
        return f"""📊 **Sistem Durumu**

📦 **İşlemler:**
• Aktif: `{running}`
• Bekleyen: `{pending}`
• Tamamlanan: `{completed}`
• Toplam: `{total}`

⚙️ **Limitler:**
• Maks. İşlem: `{MAX_PROCESSES}`
• Maks. Dosya: `{MAX_FILE_SIZE // (1024**2)}MB`"""

# ==================== FILE MANAGER ====================

class FileManager:
    def __init__(self, process_manager: ProcessManager):
        self.pm = process_manager
    
    async def save_code_file(self, file_data: bytes, file_name: str, user_id: int) -> Tuple[bool, str, Optional[str]]:
        if len(file_data) > MAX_FILE_SIZE:
            return False, f"❌ Dosya çok büyük! Maksimum {MAX_FILE_SIZE // (1024**2)}MB", None
        
        if not file_name.endswith('.py'):
            file_name += '.py'
        
        safe_name = f"{user_id}_{int(time.time())}_{file_name}"
        file_path = CODE_DIR / safe_name
        
        try:
            with open(file_path, 'wb') as f:
                f.write(file_data)
            
            file_id = hashlib.md5(safe_name.encode()).hexdigest()[:12]
            
            code_file = CodeFile(
                file_id=file_id,
                user_id=user_id,
                file_name=safe_name,
                file_path=str(file_path),
                upload_time=time.time(),
                size=len(file_data),
                hash=hashlib.md5(file_data).hexdigest()
            )
            self.pm.code_files[file_id] = code_file
            self.pm.save_state()
            
            return True, f"✅ Dosya kaydedildi: `{safe_name}`", file_id
            
        except Exception as e:
            return False, f"❌ Kayıt hatası: {str(e)}", None
    
    async def list_user_files(self, user_id: int) -> List[CodeFile]:
        return [f for f in self.pm.code_files.values() if f.user_id == user_id]
    
    async def delete_file(self, file_id: str, user_id: int) -> Tuple[bool, str]:
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
    
    async def get_file_content(self, file_id: str, user_id: int) -> Tuple[bool, str, Optional[str]]:
        if file_id not in self.pm.code_files:
            return False, "❌ Dosya bulunamadı!", None
        
        file = self.pm.code_files[file_id]
        if file.user_id != user_id and user_id not in ADMIN_IDS:
            return False, "❌ Yetkiniz yok!", None
        
        try:
            with open(file.file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return True, "", content
        except Exception as e:
            return False, f"❌ Okuma hatası: {str(e)}", None

# ==================== TELEGRAM BOT ====================

pm = ProcessManager()
fm = FileManager(pm)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_main_menu(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🚀 Kod Çalıştır", callback_data="run_code")],
        [InlineKeyboardButton("📁 Dosyalarım", callback_data="my_files")],
        [InlineKeyboardButton("📊 Aktif İşlemler", callback_data="active_processes")],
        [InlineKeyboardButton("📝 Log Göster", callback_data="show_logs")],
        [InlineKeyboardButton("📦 ZIP Yükle", callback_data="upload_zip")],
        [InlineKeyboardButton("📈 Sistem Durumu", callback_data="system_status")]
    ]
    
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"🎉 **Hoş Geldin, {user.first_name}!**\n\n"
        f"**UltimatePyRunner** - Python Kod Hosting Botu\n\n"
        f"🚀 Özellikler:\n"
        f"• Python kodlarını çalıştırma\n"
        f"• Admin onay sistemi\n"
        f"• İşlem durdurma/yeniden başlatma\n"
        f"• Dosya yönetimi\n\n"
        f"Aşağıdaki butonları kullan!",
        reply_markup=get_main_menu(is_admin(user.id)),
        parse_mode=ParseMode.MARKDOWN
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    data = query.data
    
    if data == "run_code":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Admin Onaylı", callback_data="run_approve")],
            [InlineKeyboardButton("⚡ Doğrudan Çalıştır", callback_data="run_direct")],
            [InlineKeyboardButton("◀️ Geri", callback_data="back_menu")]
        ])
        await query.edit_message_text(
            "🚀 **Çalıştırma Seçenekleri**\n\n"
            "• **Admin Onaylı:** Kod admin onayıyla çalışır\n"
            "• **Doğrudan:** Hemen çalışır",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "run_approve":
        context.user_data["run_mode"] = "approve"
        await query.edit_message_text(
            "✅ **Admin Onaylı Mod**\n\n"
            "Şimdi .py dosyasını gönder.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "run_direct":
        context.user_data["run_mode"] = "direct"
        await query.edit_message_text(
            "⚡ **Doğrudan Çalıştırma Modu**\n\n"
            "Şimdi .py dosyasını gönder.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "my_files":
        files = await fm.list_user_files(user.id)
        if not files:
            await query.edit_message_text("📁 Henüz dosyanız yok!")
            return
        
        text = "📁 **Dosyalarınız:**\n\n"
        keyboard = []
        for f in files[-10:]:
            text += f"• `{f.file_name}` ({(f.size // 1024)}KB)\n"
            keyboard.append([InlineKeyboardButton(f"📄 {f.file_name[:20]}", callback_data=f"file_{f.file_id}")])
        
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("file_"):
        file_id = data.replace("file_", "")
        file = pm.code_files.get(file_id)
        if not file:
            await query.edit_message_text("❌ Dosya bulunamadı!")
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Sil", callback_data=f"delete_{file_id}")],
            [InlineKeyboardButton("🚀 Çalıştır", callback_data=f"execute_{file_id}")],
            [InlineKeyboardButton("📄 İçerik", callback_data=f"view_{file_id}")],
            [InlineKeyboardButton("◀️ Geri", callback_data="my_files")]
        ])
        
        await query.edit_message_text(
            f"📄 **Dosya:** `{file.file_name}`\n📏 Boyut: {file.size // 1024}KB",
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
            if len(content) > 2000:
                content = content[:2000] + "\n\n... (kesildi)"
            await query.edit_message_text(f"```python\n{content}\n```", parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(error, parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("execute_"):
        file_id = data.replace("execute_", "")
        file = pm.code_files.get(file_id)
        if file:
            success, msg, pid = await pm.run_code(Path(file.file_path), user.id, file.file_name, False, context.bot)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "active_processes":
        user_processes = [p for p in pm.processes.values() if p.user_id == user.id]
        if not user_processes:
            await query.edit_message_text("📊 Aktif işleminiz yok.")
            return
        
        text = "📊 **İşlemleriniz:**\n\n"
        keyboard = []
        for p in user_processes:
            emoji = "🟢" if p.status == ProcessStatus.RUNNING else "🔴" if p.status == ProcessStatus.STOPPED else "✅" if p.status == ProcessStatus.COMPLETED else "⏳"
            text += f"{emoji} `{p.file_name}`\n   ID: `{p.process_id}`\n   Durum: {p.status.value}\n\n"
            if p.status == ProcessStatus.RUNNING:
                keyboard.append([InlineKeyboardButton(f"🛑 Durdur", callback_data=f"stop_{p.process_id}")])
        
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("stop_"):
        process_id = data.replace("stop_", "")
        success, msg = await pm.stop_process(process_id, user.id)
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "show_logs":
        user_processes = [p for p in pm.processes.values() if p.user_id == user.id]
        if not user_processes:
            await query.edit_message_text("📝 Log kaydı yok.")
            return
        
        keyboard = [[InlineKeyboardButton(f"📝 {p.file_name[:20]}", callback_data=f"log_{p.process_id}")] for p in user_processes[-5:]]
        keyboard.append([InlineKeyboardButton("◀️ Geri", callback_data="back_menu")])
        await query.edit_message_text("📝 **Log gösterilecek işlemi seç:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("log_"):
        process_id = data.replace("log_", "")
        success, log_text = await pm.get_process_log(process_id, user.id)
        await query.edit_message_text(log_text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "upload_zip":
        context.user_data["waiting_zip"] = True
        await query.edit_message_text("📦 **ZIP Arşivi Gönder**\n\nİçinde .py dosyaları olan ZIP gönder.", parse_mode=ParseMode.MARKDOWN)
    
    elif data == "system_status":
        text = pm.get_status_text()
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "admin_panel":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz!")
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏳ Bekleyen Onaylar", callback_data="pending_list")],
            [InlineKeyboardButton("📊 Tüm İşlemler", callback_data="all_processes_admin")],
            [InlineKeyboardButton("◀️ Geri", callback_data="back_menu")]
        ])
        await query.edit_message_text("👑 **Admin Paneli**", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "pending_list":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz!")
            return
        
        pending = [p for p in pm.processes.values() if p.status == ProcessStatus.PENDING_APPROVAL]
        if not pending:
            await query.edit_message_text("⏳ Bekleyen onay yok.")
            return
        
        text = "⏳ **Bekleyen Onaylar:**\n\n"
        for p in pending:
            text += f"• {p.file_name}\n  Kullanıcı: `{p.user_id}`\n  ID: `{p.process_id}`\n\n"
        
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "all_processes_admin":
        if not is_admin(user.id):
            await query.edit_message_text("❌ Yetkisiz!")
            return
        
        if not pm.processes:
            await query.edit_message_text("📊 Hiç işlem yok.")
            return
        
        text = "📊 **Tüm İşlemler:**\n\n"
        for p in list(pm.processes.values())[-15:]:
            text += f"• {p.file_name}\n  Kullanıcı: `{p.user_id}` | Durum: {p.status.value}\n  ID: `{p.process_id}`\n\n"
        
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "back_menu":
        await query.edit_message_text(
            "🏠 **Ana Menü**",
            reply_markup=get_main_menu(is_admin(user.id)),
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    document = update.message.document
    
    if not document:
        return
    
    # ZIP işleme
    if context.user_data.get("waiting_zip"):
        if document.file_name.endswith('.zip'):
            file = await context.bot.get_file(document.file_id)
            zip_data = await file.download_as_bytearray()
            
            try:
                with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
                    tmp.write(zip_data)
                    tmp_path = tmp.name
                
                extract_dir = TEMP_DIR / f"extract_{user.id}_{int(time.time())}"
                extract_dir.mkdir(exist_ok=True)
                
                with zipfile.ZipFile(tmp_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                
                count = 0
                for py_file in extract_dir.rglob('*.py'):
                    with open(py_file, 'rb') as f:
                        content = f.read()
                    success, msg, fid = await fm.save_code_file(content, py_file.name, user.id)
                    if success:
                        count += 1
                
                shutil.rmtree(extract_dir)
                os.unlink(tmp_path)
                
                await update.message.reply_text(f"✅ {count} Python dosyası yüklendi!")
                
            except Exception as e:
                await update.message.reply_text(f"❌ ZIP hatası: {str(e)}")
            
            context.user_data["waiting_zip"] = False
        else:
            await update.message.reply_text("❌ Lütfen geçerli bir ZIP dosyası gönder!")
        return
    
    # Python dosyası çalıştırma
    if document.file_name.endswith('.py'):
        file = await context.bot.get_file(document.file_id)
        file_data = await file.download_as_bytearray()
        
        success, msg, file_id = await fm.save_code_file(bytes(file_data), document.file_name, user.id)
        
        if success and file_id:
            file_path = CODE_DIR / [f for f in pm.code_files.values() if f.file_id == file_id][0].file_name
            requires_approval = context.user_data.get("run_mode") == "approve"
            run_success, run_msg, proc_id = await pm.run_code(file_path, user.id, document.file_name, requires_approval, context.bot)
            await update.message.reply_text(f"{msg}\n\n{run_msg}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        if "run_mode" in context.user_data:
            del context.user_data["run_mode"]
    else:
        await update.message.reply_text("❌ Sadece .py veya .zip dosyaları gönderebilirsiniz!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ İşlem iptal edildi!")

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) == 0:
        await update.message.reply_text("❌ Kullanım: `/log <process_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    process_id = context.args[0]
    success, log_text = await pm.get_process_log(process_id, user.id)
    await update.message.reply_text(log_text, parse_mode=ParseMode.MARKDOWN)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) == 0:
        await update.message.reply_text("❌ Kullanım: `/stop <process_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    process_id = context.args[0]
    success, msg = await pm.stop_process(process_id, user.id)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ==================== ANA FONKSİYON ====================

async def main():
    """Ana bot fonksiyonu - Render uyumlu"""
    global application
    
    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )
    
    # Komutlar
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("log", log_command))
    application.add_handler(CommandHandler("stop", stop_command))
    
    # Mesaj handlerları
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Botu başlat
    await application.initialize()
    await application.start()
    
    # Polling başlat - Render için doğru yöntem
    await application.updater.start_polling()
    
    # Botu canlı tut
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

# ==================== ÇALIŞTIR ====================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot durduruldu")
    except Exception as e:
        logger.error(f"Bot hatası: {e}")
        traceback.print_exc()
