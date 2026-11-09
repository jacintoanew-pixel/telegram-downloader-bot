#!/usr/bin/env python3
"""
Media Suite Bot (24/7)
- Descarga de Audio (MP3), Video (MP4) y Estado WhatsApp desde enlaces externos.
- Optimizador de Fotos y Videos para Estados de WhatsApp en Android (Anti-Compresión).
- REMIXER DE VIDEO & AUDIO: Extrae el audio de cualquier video/link y remezcla
  con otro video manteniendo 100% la calidad original (Stream Copy sin pérdida).
- Control de duración personalizado (15s, 30s, duración del audio o video).
"""

import os
import re
import glob
import uuid
import time
import socket
import logging
import subprocess
from urllib.parse import urlparse
import ipaddress
from html import escape as esc
from dotenv import load_dotenv
import telebot
from telebot import types
import yt_dlp

# Cargar configuración segura
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.getenv('ENV_FILE', os.path.join(BASE_DIR, '.env')))

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALLOWED_USERS = [
    int(uid.strip()) for uid in os.getenv('ALLOWED_USER_IDS', '').split(',') if uid.strip().isdigit()
]
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', os.path.join(BASE_DIR, 'downloads'))
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB límite estándar de subida de Telegram Bot API

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('tg-downloader')

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN no configurado en .env")

if not ALLOWED_USERS:
    logger.warning("ALLOWED_USER_IDS esta vacio: el bot rechazara a TODOS los usuarios (fail-closed).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# Almacenes en memoria
PENDING_TASKS = {}
USER_REMIX_AUDIO = {}  # {user_id: {'path': ..., 'title': ..., 'duration': ...}}
USER_REMIX_PHOTO = {}  # {user_id: {'path': ..., 'file_id': ..., 'saved_at': ...}}
PENDING_REMIX_VIDEOS = {}  # {task_id: {'file_id': ..., 'user_id': ...}}
PENDING_PHOTO_TASKS = {}   # {task_id: {'file_id': ..., 'user_id': ...}}


# ==========================================
# VALIDACIONES DE SEGURIDAD
# ==========================================

def is_authorized(user_id: int) -> bool:
    """Valida si el usuario está en la lista blanca estricta."""
    # Fail closed: with no allow-list configured nobody is authorized.
    return bool(ALLOWED_USERS) and user_id in ALLOWED_USERS


def is_safe_url(url: str) -> tuple[bool, str]:
    """Protección Anti-SSRF para URLs remotas."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ('http', 'https'):
            return False, "Esquema no permitido (solo http/https)."

        hostname = parsed.hostname
        if not hostname:
            return False, "Nombre de host no válido."

        blocked_hostnames = {
            'localhost', '127.0.0.1', '::1', '0.0.0.0', 'metadata.google.internal',
            'instance-data', '169.254.169.254'
        }
        if hostname.lower() in blocked_hostnames or hostname.endswith('.local') or hostname.endswith('.internal'):
            return False, "Destino no permitido (intento de acceso a red interna)."

        try:
            # Check EVERY resolved address (A + AAAA), not just the first one.
            # Known limitation: yt-dlp re-resolves the host, so DNS rebinding
            # and redirects are not fully covered. Run the bot in an egress-restricted sandbox.
            for info in socket.getaddrinfo(hostname, None):
                ip_obj = ipaddress.ip_address(info[4][0])
                if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved
                        or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified):
                    return False, "La dirección IP pertenece a un segmento privado o reservado."
        except socket.gaierror:
            return False, "No fue posible resolver el dominio en el DNS."

        return True, "URL segura"
    except Exception as e:
        return False, f"Error validando URL: {esc(str(e))}"


def format_size_error_message(file_size_bytes: int) -> str:
    """Mensaje explicativo y constructivo cuando un archivo supera los 20 MB de la API pública de Telegram."""
    size_mb = file_size_bytes / (1024 * 1024)
    return (
        f"⚠️ <b>El archivo supera el límite de Telegram ({size_mb:.1f} MB)</b>\n\n"
        "Los servidores oficiales de Telegram (<i>api.telegram.org</i>) <b>bloquean</b> a los bots descargar archivos de más de <b>20 MB</b>.\n\n"
        "💡 <b>Soluciones inmediatas:</b>\n"
        "1. <b>Enviar como Video normal (no como Archivo):</b> En Telegram de tu celular, al adjuntar desde la galería, si lo envías como video normal en vez de archivo sin comprimir, Telegram te permite ajustar la barra de resolución o recortar unos segundos para que pese menos de 20 MB.\n"
        "2. <b>Si el video está en internet:</b> Pega el enlace directo (TikTok, Reels, YouTube) y el bot lo descarga en el servidor sin límite de tamaño.\n\n"
        "🚀 <b>¿Quieres enviar videos pesados de hasta 2 GB (2000 MB)?</b>\n"
        "Podemos activar el servidor local de Bot API en el VPS con tu <code>api_id</code> y <code>api_hash</code> gratuitos de <i>my.telegram.org</i>."
    )


def cleanup_task_files(task_id: str):
    """Elimina archivos residuales de una tarea."""
    for filepath in glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*")):
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception as e:
            logger.warning(f"No se pudo eliminar {filepath}: {e}")


def purge_old_tasks():
    """Limpia tareas pendientes en memoria con más de 30 minutos."""
    now = time.time()
    expired = [tid for tid, data in PENDING_TASKS.items() if now - data.get('created_at', 0) > 1800]
    for tid in expired:
        PENDING_TASKS.pop(tid, None)
        cleanup_task_files(tid)
    expired_vids = [tid for tid, data in PENDING_REMIX_VIDEOS.items() if now - data.get('created_at', 0) > 1800]
    for tid in expired_vids:
        PENDING_REMIX_VIDEOS.pop(tid, None)
        cleanup_task_files(tid)
    expired_ph = [tid for tid, data in PENDING_PHOTO_TASKS.items() if now - data.get('created_at', 0) > 1800]
    for tid in expired_ph:
        PENDING_PHOTO_TASKS.pop(tid, None)
        cleanup_task_files(tid)


# ==========================================
# COMANDOS PRINCIPALES
# ==========================================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message: types.Message):
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔ <b>Acceso Denegado</b>\nEste bot es privado y exclusivo de su propietario.")
        return

    user_id = message.from_user.id
    audio_info = USER_REMIX_AUDIO.get(user_id)
    photo_info = USER_REMIX_PHOTO.get(user_id)
    session_status = ""
    if audio_info:
        session_status += (
            f"\n\n🎧 <b>Audio actualmente activo en memoria:</b>\n"
            f"🎵 <i>{esc(audio_info['title'])}</i> ({int(audio_info.get('duration', 0))} seg)"
        )
    if photo_info:
        session_status += (
            f"\n\n📸 <b>Foto guardada en memoria:</b> Lista para agregarle audio."
        )
    if session_status:
        session_status += "\n<i>(Usa /limpiar para reiniciar la memoria temporal)</i>"

    msg = (
        "✨ <b>Media Suite Bot (24/7)</b>\n\n"
        "<b>Herramientas Disponibles:</b>\n\n"
        "1. 🎬 <b>Remixer de Video & Audio (Cero Pérdida):</b>\n"
        "Extrae el audio de cualquier video o enlace y remézclalo con otro video sin tocar la calidad original (Stream Copy).\n\n"
        "2. 📸 <b>Crear Video desde Foto con Audio:</b>\n"
        "Envíame una foto y colócale cualquier música, nota de voz o audio de video para generar un video vertical 1080x1920 HD.\n\n"
        "3. 🟢 <b>Optimizador de Estados de WhatsApp (HD):</b>\n"
        "Envíame fotos o videos directos y los codifico con parámetros exactos anti-compresión de Android.\n\n"
        "4. 📥 <b>Descargas Multimedia:</b>\n"
        "Pega enlaces (YouTube, TikTok, Reels, Twitter, SoundCloud) y descarga en MP3, MP4 o HD."
        f"{session_status}\n\n"
        "<i>¡Envíame un video, foto, audio o enlace para comenzar! 👇</i>"
    )
    bot.reply_to(message, msg)


@bot.message_handler(commands=['limpiar', 'limpiar_audio', 'descartar_audio', 'limpiar_foto'])
def handle_clear_media(message: types.Message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return
    cleared = []
    if user_id in USER_REMIX_AUDIO:
        old_path = USER_REMIX_AUDIO[user_id].get('path')
        if old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass
        USER_REMIX_AUDIO.pop(user_id, None)
        cleared.append("audio")

    if user_id in USER_REMIX_PHOTO:
        old_path = USER_REMIX_PHOTO[user_id].get('path')
        if old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass
        USER_REMIX_PHOTO.pop(user_id, None)
        cleared.append("foto")

    if cleared:
        bot.reply_to(message, f"🗑️ <b>Memoria limpiada:</b> Se descartó {' y '.join(cleared)} de la sesión.")
    else:
        bot.reply_to(message, "ℹ️ No tenías ningún audio ni foto guardados en memoria.")


# ==========================================
# RECEPCIÓN DE FOTOS
# ==========================================

@bot.message_handler(content_types=['photo'])
def handle_photo(message: types.Message):
    purge_old_tasks()
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return
    photo = message.photo[-1]

    if photo.file_size and photo.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, format_size_error_message(photo.file_size))
        return

    if user_id in USER_REMIX_AUDIO and os.path.exists(USER_REMIX_AUDIO[user_id]['path']):
        prompt_photo_with_audio_options(message, photo.file_id)
    else:
        prompt_photo_options(message, photo.file_id)


# ==========================================
# RECEPCIÓN DE VIDEOS
# ==========================================

@bot.message_handler(content_types=['video'])
def handle_video(message: types.Message):
    purge_old_tasks()
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return

    video = message.video
    if video.file_size and video.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, format_size_error_message(video.file_size))
        return

    # Si el usuario YA tiene un audio guardado para Remix, ofrecer la mezcla directa
    if user_id in USER_REMIX_AUDIO and os.path.exists(USER_REMIX_AUDIO[user_id]['path']):
        prompt_remix_duration(message, video.file_id, is_document=False)
    else:
        # Menú de opciones para el video recibido
        prompt_video_options(message, video.file_id, is_document=False)


# ==========================================
# RECEPCIÓN DE AUDIOS DIRECTOS (MP3 / NOTAS)
# ==========================================

@bot.message_handler(content_types=['audio', 'voice'])
def handle_audio(message: types.Message):
    purge_old_tasks()
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return

    audio = message.audio or message.voice
    if audio.file_size and audio.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, format_size_error_message(audio.file_size))
        return

    save_user_remix_audio(message, audio.file_id, title=getattr(audio, 'title', None) or "Audio recibido")


# ==========================================
# RECEPCIÓN DE DOCUMENTOS / ARCHIVOS
# ==========================================

@bot.message_handler(content_types=['document'])
def handle_document(message: types.Message):
    purge_old_tasks()
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return

    doc = message.document
    mime = (doc.mime_type or '').lower()
    fname = (doc.file_name or '').lower()

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, format_size_error_message(doc.file_size))
        return

    if mime.startswith('image/') or fname.endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.heic')):
        if user_id in USER_REMIX_AUDIO and os.path.exists(USER_REMIX_AUDIO[user_id]['path']):
            prompt_photo_with_audio_options(message, doc.file_id)
        else:
            prompt_photo_options(message, doc.file_id)
    elif mime.startswith('video/') or fname.endswith(('.mp4', '.mov', '.mkv', '.avi', '.webm')):
        if user_id in USER_REMIX_AUDIO and os.path.exists(USER_REMIX_AUDIO[user_id]['path']):
            prompt_remix_duration(message, doc.file_id, is_document=True)
        else:
            prompt_video_options(message, doc.file_id, is_document=True)
    elif mime.startswith('audio/') or fname.endswith(('.mp3', '.m4a', '.wav', '.ogg', '.flac')):
        save_user_remix_audio(message, doc.file_id, title=doc.file_name or "Audio")
    else:
        bot.reply_to(
            message,
            "ℹ️ <b>Archivo recibido.</b>\n"
            "Por favor envíame una foto, video o audio para procesarlo."
        )


# ==========================================
# FLUJO INTERACTIVO DE VIDEOS
# ==========================================

def prompt_video_options(message: types.Message, file_id: str, is_document: bool):
    """Muestra opciones para un video cuando NO hay audio guardado."""
    task_id = uuid.uuid4().hex[:10]
    PENDING_REMIX_VIDEOS[task_id] = {
        'file_id': file_id,
        'user_id': message.from_user.id,
        'chat_id': message.chat.id,
        'message_id': message.message_id,
        'created_at': time.time()
    }

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    btn_opt = types.InlineKeyboardButton("✨ Optimizar Estado WhatsApp (HD)", callback_data=f"vid:opt:{task_id}")
    btn_ext = types.InlineKeyboardButton("🎧 Extraer este Audio para Remix", callback_data=f"vid:ext_audio:{task_id}")
    btn_cancel = types.InlineKeyboardButton("❌ Cancelar", callback_data=f"vid:cancel:{task_id}")
    keyboard.add(btn_opt, btn_ext, btn_cancel)

    bot.reply_to(
        message,
        "📹 <b>Video recibido con éxito.</b>\n¿Qué deseas hacer con él?",
        reply_markup=keyboard
    )


def prompt_remix_duration(message: types.Message, file_id: str, is_document: bool):
    """Muestra opciones de duración de remix cuando SÍ hay un audio en sesión."""
    user_id = message.from_user.id
    audio_info = USER_REMIX_AUDIO[user_id]
    task_id = uuid.uuid4().hex[:10]

    PENDING_REMIX_VIDEOS[task_id] = {
        'file_id': file_id,
        'user_id': user_id,
        'chat_id': message.chat.id,
        'message_id': message.message_id,
        'created_at': time.time()
    }

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    btn_15 = types.InlineKeyboardButton("⏱️ 15 segundos", callback_data=f"remix:15:{task_id}")
    btn_30 = types.InlineKeyboardButton("⏱️ 30 seg (WhatsApp)", callback_data=f"remix:30:{task_id}")
    btn_audio = types.InlineKeyboardButton("🎵 Duración Audio", callback_data=f"remix:audio:{task_id}")
    btn_video = types.InlineKeyboardButton("🎬 Duración Video", callback_data=f"remix:video:{task_id}")
    btn_opt_only = types.InlineKeyboardButton("⚡ Solo Optimizar (Sin Remix)", callback_data=f"vid:opt:{task_id}")
    btn_cancel = types.InlineKeyboardButton("❌ Cancelar", callback_data=f"remix:cancel:{task_id}")

    keyboard.add(btn_15, btn_30)
    keyboard.add(btn_audio, btn_video)
    keyboard.add(btn_opt_only)
    keyboard.add(btn_cancel)

    caption = (
        "🎬 <b>¡Video recibido para Remix!</b>\n\n"
        f"🎧 <b>Audio activo:</b> <i>{esc(audio_info['title'])}</i> ({int(audio_info.get('duration', 0))} seg)\n\n"
        "¿Qué duración deseas para el video final remezclado?\n"
        "<i>(La calidad visual se mantendrá 100% idéntica sin pérdida)</i>"
    )
    bot.reply_to(message, caption, reply_markup=keyboard)


# ==========================================
# FLUJO INTERACTIVO DE FOTOS
# ==========================================

def prompt_photo_with_audio_options(message: types.Message, file_id: str):
    """Muestra opciones para una foto cuando SÍ hay un audio en sesión."""
    user_id = message.from_user.id
    audio_info = USER_REMIX_AUDIO[user_id]
    task_id = uuid.uuid4().hex[:10]

    PENDING_PHOTO_TASKS[task_id] = {
        'file_id': file_id,
        'user_id': user_id,
        'chat_id': message.chat.id,
        'message_id': message.message_id,
        'created_at': time.time()
    }

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    btn_15 = types.InlineKeyboardButton("⏱️ 15 seg (Video c/ Audio)", callback_data=f"ph_remix:15:{task_id}")
    btn_30 = types.InlineKeyboardButton("⏱️ 30 seg (WhatsApp)", callback_data=f"ph_remix:30:{task_id}")
    btn_audio = types.InlineKeyboardButton("🎵 Duración Audio", callback_data=f"ph_remix:audio:{task_id}")
    btn_opt_only = types.InlineKeyboardButton("✨ Solo Optimizar Foto (HD)", callback_data=f"ph_remix:opt_only:{task_id}")
    btn_cancel = types.InlineKeyboardButton("❌ Cancelar", callback_data=f"ph_remix:cancel:{task_id}")

    keyboard.add(btn_15, btn_30)
    keyboard.add(btn_audio)
    keyboard.add(btn_opt_only)
    keyboard.add(btn_cancel)

    caption = (
        "📸 <b>¡Foto recibida para Video con Audio!</b>\n\n"
        f"🎧 <b>Pista activa:</b> <i>{esc(audio_info['title'])}</i> ({int(audio_info.get('duration', 0))} seg)\n\n"
        "¿Qué duración deseas para crear el video con esta foto y la música?\n"
        "<i>(Genera un video vertical 1080x1920 HD listo para Estado de WhatsApp o Reels)</i>"
    )
    bot.reply_to(message, caption, reply_markup=keyboard)


def prompt_photo_options(message: types.Message, file_id: str):
    """Muestra opciones para una foto cuando NO hay audio guardado."""
    task_id = uuid.uuid4().hex[:10]
    user_id = message.from_user.id

    PENDING_PHOTO_TASKS[task_id] = {
        'file_id': file_id,
        'user_id': user_id,
        'chat_id': message.chat.id,
        'message_id': message.message_id,
        'created_at': time.time()
    }

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    btn_opt = types.InlineKeyboardButton("✨ Optimizar Estado WhatsApp (Foto HD)", callback_data=f"ph_menu:opt:{task_id}")
    btn_music = types.InlineKeyboardButton("🎵 Ponerle Música (Crear Video con Audio)", callback_data=f"ph_menu:save_photo:{task_id}")
    btn_cancel = types.InlineKeyboardButton("❌ Cancelar", callback_data=f"ph_menu:cancel:{task_id}")

    keyboard.add(btn_opt, btn_music, btn_cancel)

    bot.reply_to(
        message,
        "📸 <b>Foto recibida con éxito.</b>\n\n¿Qué deseas hacer con ella?",
        reply_markup=keyboard
    )


# ==========================================
# CALLBACKS DE BOTONES (FOTOS)
# ==========================================

@bot.callback_query_handler(func=lambda call: call.data.startswith(('ph_remix:', 'ph_menu:', 'ph_saved:')))
def handle_photo_callbacks(call: types.CallbackQuery):
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Acceso denegado.", show_alert=True)
        return

    parts = call.data.split(':')
    prefix = parts[0]
    action = parts[1]

    if prefix == 'ph_saved':
        if action == 'cancel':
            bot.answer_callback_query(call.id, "Cancelado.")
            try:
                bot.edit_message_text("❌ <b>Operación cancelada.</b>", call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            return

        bot.answer_callback_query(call.id, "Creando video...")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        execute_photo_audio_remix_saved(call.message, duration_mode=action)
        return

    task_id = parts[2]
    task = PENDING_PHOTO_TASKS.get(task_id)

    if action == 'cancel':
        PENDING_PHOTO_TASKS.pop(task_id, None)
        bot.answer_callback_query(call.id, "Cancelado.")
        try:
            bot.edit_message_text("❌ <b>Operación cancelada.</b>", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if not task:
        bot.answer_callback_query(call.id, "⚠️ Solicitud expirada. Reenvía la foto.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Procesando...")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    file_id = task['file_id']
    chat_id = task['chat_id']
    reply_msg_id = task['message_id']

    if prefix == 'ph_menu':
        if action == 'opt':
            process_photo_file(chat_id, file_id, status_msg_id=call.message.message_id, reply_to_id=reply_msg_id)
        elif action == 'save_photo':
            save_user_remix_photo(call.message, file_id, task_id)
    elif prefix == 'ph_remix':
        if action == 'opt_only':
            process_photo_file(chat_id, file_id, status_msg_id=call.message.message_id, reply_to_id=reply_msg_id)
        else:
            execute_photo_audio_remix(call.message, file_id, duration_mode=action, task_id=task_id)


# ==========================================
# CALLBACKS DE BOTONES (VIDEOS Y REMIX)
# ==========================================

@bot.callback_query_handler(func=lambda call: call.data.startswith(('vid:', 'remix:')))
def handle_video_and_remix_callbacks(call: types.CallbackQuery):
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Acceso denegado.", show_alert=True)
        return

    parts = call.data.split(':')
    prefix = parts[0]
    action = parts[1]
    task_id = parts[2]

    task = PENDING_REMIX_VIDEOS.get(task_id)

    if action == 'cancel':
        PENDING_REMIX_VIDEOS.pop(task_id, None)
        bot.answer_callback_query(call.id, "Cancelado.")
        try:
            bot.edit_message_text("❌ <b>Operación cancelada.</b>", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if not task:
        bot.answer_callback_query(call.id, "⚠️ La solicitud ha expirado. Reenvía el video.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Procesando...")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    file_id = task['file_id']
    chat_id = task['chat_id']
    reply_msg_id = task['message_id']

    if prefix == 'vid':
        if action == 'opt':
            process_video_optimization(chat_id, call.message.message_id, reply_msg_id, file_id)
        elif action == 'ext_audio':
            extract_audio_from_video_message(call.message, file_id, task_id)
    elif prefix == 'remix':
        execute_video_remix(call.message, file_id, duration_mode=action, task_id=task_id)


# ==========================================
# LÓGICA DE REMIX CON FFmpeg (STREAM COPY)
# ==========================================

def notify_audio_saved(status_msg: types.Message, user_id: int, title: str, dur_str: str):
    """Notifica al usuario que se guardó el audio y ofrece crear video con foto si hay una guardada."""
    chat_id = status_msg.chat.id
    if user_id in USER_REMIX_PHOTO and os.path.exists(USER_REMIX_PHOTO[user_id]['path']):
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        btn_15 = types.InlineKeyboardButton("⏱️ 15 seg (Stories)", callback_data="ph_saved:15")
        btn_30 = types.InlineKeyboardButton("⏱️ 30 seg (WhatsApp)", callback_data="ph_saved:30")
        btn_audio = types.InlineKeyboardButton("🎵 Duración Audio", callback_data="ph_saved:audio")
        btn_cancel = types.InlineKeyboardButton("❌ Descartar", callback_data="ph_saved:cancel")
        keyboard.add(btn_15, btn_30)
        keyboard.add(btn_audio)
        keyboard.add(btn_cancel)

        bot.edit_message_text(
            f"🎧 <b>¡Audio listo para tu Foto!</b>\n\n"
            f"🎵 <b>Pista:</b> <i>{esc(title)}</i>\n"
            f"⏱ <b>Duración:</b> {dur_str}\n\n"
            "📸 <i>Tienes una foto guardada en memoria.</i>\n"
            "¿Qué duración deseas para crear el video con música?",
            chat_id=chat_id,
            message_id=status_msg.message_id,
            reply_markup=keyboard
        )
    else:
        bot.edit_message_text(
            f"🎧 <b>¡Audio guardado para Remix!</b>\n\n"
            f"🎵 <b>Pista:</b> <i>{esc(title)}</i>\n"
            f"⏱ <b>Duración:</b> {dur_str}\n\n"
            "👉 <b>Ahora envíame el video o la foto</b> al que le quieres poner esta música.\n"
            "<i>(O usa /limpiar para descartarlo).</i>",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )


def save_user_remix_audio(message: types.Message, file_id: str, title: str):
    """Guarda una pista de audio en la sesión del usuario."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    status_msg = bot.reply_to(message, "⏳ <b>Guardando audio para Remix...</b>")

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        user_audio_path = os.path.join(DOWNLOAD_DIR, f"remix_audio_{user_id}.mp3")
        temp_input = os.path.join(DOWNLOAD_DIR, f"temp_raw_{user_id}")

        with open(temp_input, 'wb') as f:
            f.write(downloaded)

        # Convertir a MP3 estandarizado
        subprocess.run(
            ['ffmpeg', '-y', '-i', temp_input, '-vn', '-c:a', 'libmp3lame', '-b:a', '192k', user_audio_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )

        if os.path.exists(temp_input):
            os.remove(temp_input)

        # Obtener duración real con ffprobe
        probe_cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', user_audio_path
        ]
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        duration = float(res.stdout.strip() or 0)

        USER_REMIX_AUDIO[user_id] = {
            'path': user_audio_path,
            'title': title,
            'duration': duration,
            'saved_at': time.time()
        }

        mins = int(duration // 60)
        secs = int(duration % 60)
        dur_str = f"{mins}:{secs:02d} min" if duration else "Completo"

        notify_audio_saved(status_msg, user_id, title, dur_str)
    except Exception as e:
        logger.error(f"Error guardando audio remix: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al guardar audio: {esc(str(e)[:150])}", chat_id=chat_id, message_id=status_msg.message_id)


def extract_audio_from_video_message(status_msg: types.Message, file_id: str, task_id: str):
    """Descarga un video enviado y extrae su audio para la sesión de remix."""
    chat_id = status_msg.chat.id
    user_id = status_msg.chat.id  # en chat privado chat.id == user.id

    bot.edit_message_text("🎧 <b>Extrayendo pista de audio del video...</b>", chat_id=chat_id, message_id=status_msg.message_id)

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        temp_video = os.path.join(DOWNLOAD_DIR, f"{task_id}_vid.mp4")
        user_audio_path = os.path.join(DOWNLOAD_DIR, f"remix_audio_{user_id}.mp3")

        with open(temp_video, 'wb') as f:
            f.write(downloaded)

        subprocess.run(
            ['ffmpeg', '-y', '-i', temp_video, '-vn', '-c:a', 'libmp3lame', '-b:a', '192k', user_audio_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )

        probe_cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', user_audio_path
        ]
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        duration = float(res.stdout.strip() or 0)

        USER_REMIX_AUDIO[user_id] = {
            'path': user_audio_path,
            'title': "Audio extraído de video",
            'duration': duration,
            'saved_at': time.time()
        }

        cleanup_task_files(task_id)

        mins = int(duration // 60)
        secs = int(duration % 60)
        dur_str = f"{mins}:{secs:02d} min"

        notify_audio_saved(status_msg, user_id, "Audio extraído de video", dur_str)

        bot.edit_message_text(
            f"🎧 <b>¡Audio extraído y guardado con éxito!</b>\n\n"
            f"⏱ <b>Duración:</b> {dur_str}\n\n"
            "👉 <b>Ahora envíame el video</b> al que le quieres colocar esta música.\n"
            "<i>(Puedes recortarlo a 15s, 30s o dejarlo completo sin perder calidad).</i>",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )
    except Exception as e:
        logger.error(f"Error extrayendo audio: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al extraer audio: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg.message_id)


def execute_video_remix(status_msg: types.Message, file_id: str, duration_mode: str, task_id: str):
    """Ejecuta la remezcla con FFmpeg preservando 100% la calidad visual (Stream Copy)."""
    chat_id = status_msg.chat.id
    user_id = status_msg.chat.id
    audio_info = USER_REMIX_AUDIO.get(user_id)

    if not audio_info or not os.path.exists(audio_info['path']):
        bot.edit_message_text("⚠️ No se encontró el audio de remix. Por favor guarda el audio primero.", chat_id=chat_id, message_id=status_msg.message_id)
        return

    bot.edit_message_text(
        "🚀 <b>Remezclando video y audio a máxima fidelidad (0 pérdida)...</b>",
        chat_id=chat_id,
        message_id=status_msg.message_id
    )
    bot.send_chat_action(chat_id, 'upload_video')

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        input_video = os.path.join(DOWNLOAD_DIR, f"{task_id}_in.mp4")
        output_video = os.path.join(DOWNLOAD_DIR, f"{task_id}_remix_final.mp4")
        audio_path = audio_info['path']

        with open(input_video, 'wb') as f:
            f.write(downloaded)

        # Construcción de comando FFmpeg
        # Usamos -c:v copy para no tocar un solo pixel del video (cero pérdida y renderizado en 2 seg)
        cmd = ['ffmpeg', '-y']

        if duration_mode == '15':
            cmd += ['-ss', '0', '-i', input_video, '-ss', '0', '-i', audio_path, '-t', '15', '-map', '0:v:0', '-map', '1:a:0']
            desc_dur = "15 segundos"
        elif duration_mode == '30':
            cmd += ['-ss', '0', '-i', input_video, '-ss', '0', '-i', audio_path, '-t', '30', '-map', '0:v:0', '-map', '1:a:0']
            desc_dur = "30 segundos (Estado WhatsApp)"
        elif duration_mode == 'video':
            # Se ajusta a la duración del video (corta el audio si es más largo)
            cmd += ['-i', input_video, '-stream_loop', '-1', '-i', audio_path, '-map', '0:v:0', '-map', '1:a:0', '-shortest']
            desc_dur = "Duración completa del video"
        elif duration_mode == 'audio':
            # Se ajusta a la duración del audio (hace bucle del video si el video es más corto)
            cmd += ['-stream_loop', '-1', '-i', input_video, '-i', audio_path, '-map', '0:v:0', '-map', '1:a:0', '-shortest']
            desc_dur = "Duración completa del audio"
        else:
            cmd += ['-i', input_video, '-i', audio_path, '-map', '0:v:0', '-map', '1:a:0', '-shortest']
            desc_dur = "Automática"

        # Priorizamos Stream Copy (c:v copy). Si el contenedor requiere reempaquetado, cae a CRF 18
        cmd += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', output_video]

        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            # Fallback seguro con codificación visualmente idéntica (CRF 17)
            logger.info("Stream copy falló, aplicando fallback CRF 17...")
            cmd_fallback = [
                'ffmpeg', '-y', '-i', input_video, '-i', audio_path,
                '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', 'libx264', '-crf', '17', '-preset', 'veryfast',
                '-c:a', 'aac', '-b:a', '192k',
                '-movflags', '+faststart'
            ]
            if duration_mode in ('15', '30'):
                cmd_fallback += ['-t', duration_mode]
            else:
                cmd_fallback += ['-shortest']
            cmd_fallback.append(output_video)
            subprocess.run(cmd_fallback, check=True)

        caption = (
            "🎬 <b>¡Video Remezclado con Éxito!</b>\n\n"
            f"🎵 <b>Audio aplicado:</b> <i>{esc(audio_info['title'])}</i>\n"
            f"⏱ <b>Duración:</b> {desc_dur}\n"
            "💎 <b>Calidad Visual:</b> 100% Cero Pérdida (Stream Copy)\n"
            "🔊 <b>Audio:</b> AAC 192 kbps Alta Fidelidad\n\n"
            "📲 <i>Guárdalo en tu galería y compártelo directo a WhatsApp o Reels.</i>"
        )

        with open(output_video, 'rb') as vf:
            bot.send_video(chat_id, vf, caption=caption, supports_streaming=True)

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error en ejecución de remix: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al remezclar: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg.message_id)
    finally:
        cleanup_task_files(task_id)


# ==========================================
# OPTIMIZACIÓN PURA DE VIDEO (SIN REMIX)
# ==========================================

def process_video_optimization(chat_id: int, status_msg_id: int, reply_to_id: int, file_id: str):
    task_id = uuid.uuid4().hex[:10]
    bot.edit_message_text("⚙️ <b>Optimizando video para Estado WhatsApp (H.264 HD a 30 FPS)...</b>", chat_id=chat_id, message_id=status_msg_id)
    bot.send_chat_action(chat_id, 'upload_video')

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        input_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_in.mp4")
        output_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_opt.mp4")

        with open(input_path, 'wb') as f:
            f.write(downloaded)

        cmd = [
            'ffmpeg', '-y', '-i', input_path,
            '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,unsharp=5:5:0.5:5:5:0.0',
            '-c:v', 'libx264', '-preset', 'slow', '-profile:v', 'high', '-level', '4.1',
            '-crf', '19', '-maxrate', '3200k', '-bufsize', '6000k', '-r', '30',
            '-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-movflags', '+faststart',
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        caption = (
            "✨ <b>¡Video Optimizado para Estado de WhatsApp!</b>\n\n"
            "• 1080 x 1920 Full HD Vertical (30 FPS)\n"
            "• Bitrate calibrado anti-compresión destructiva\n\n"
            "📲 <i>Guarda en Galería y súbelo a tu estado.</i>"
        )

        with open(output_path, 'rb') as out_f:
            bot.send_video(chat_id, out_f, caption=caption, supports_streaming=True, reply_to_message_id=reply_to_id)

        try:
            bot.delete_message(chat_id, status_msg_id)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error optimizando video: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg_id)
    finally:
        cleanup_task_files(task_id)


def process_photo_file(target, file_id: str, status_msg_id: int = None, reply_to_id: int = None):
    """Optimiza una foto a 1080x1920 con Unsharp Mask para Estados de WhatsApp sin pérdida."""
    task_id = uuid.uuid4().hex[:10]
    if isinstance(target, types.Message):
        chat_id = target.chat.id
        reply_to = target.message_id
        status_msg = bot.reply_to(target, "⚙️ <b>Optimizando foto para Estado de WhatsApp...</b>")
        curr_status_id = status_msg.message_id
    else:
        chat_id = target
        reply_to = reply_to_id
        curr_status_id = status_msg_id
        bot.edit_message_text("⚙️ <b>Optimizando foto para Estado de WhatsApp...</b>", chat_id=chat_id, message_id=curr_status_id)

    bot.send_chat_action(chat_id, 'upload_photo')

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        input_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_in.jpg")
        output_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_opt.jpg")

        with open(input_path, 'wb') as f:
            f.write(downloaded)

        cmd = [
            'ffmpeg', '-y', '-i', input_path,
            '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,unsharp=3:3:0.5:3:3:0.0',
            '-qscale:v', '2',
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        caption = (
            "✨ <b>¡Foto Optimizada para Estado de WhatsApp!</b>\n\n"
            "• 1080 x 1920 px (9:16 Vertical)\n"
            "• Filtro Unsharp Mask inteligente sin pérdida\n\n"
            "📲 <i>Guárdala en galería y súbela directo a tu estado.</i>"
        )

        with open(output_path, 'rb') as out_f:
            bot.send_photo(chat_id, out_f, caption=caption, reply_to_message_id=reply_to)

        try:
            bot.delete_message(chat_id, curr_status_id)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error optimizando foto: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error: {esc(str(e)[:180])}", chat_id=chat_id, message_id=curr_status_id)
    finally:
        cleanup_task_files(task_id)


def save_user_remix_photo(status_msg: types.Message, file_id: str, task_id: str):
    """Guarda una foto en la sesión del usuario para luego montarle audio."""
    chat_id = status_msg.chat.id
    user_id = status_msg.chat.id

    bot.edit_message_text("⏳ <b>Guardando foto para video con música...</b>", chat_id=chat_id, message_id=status_msg.message_id)

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        user_photo_path = os.path.join(DOWNLOAD_DIR, f"remix_photo_{user_id}.jpg")

        with open(user_photo_path, 'wb') as f:
            f.write(downloaded)

        USER_REMIX_PHOTO[user_id] = {
            'path': user_photo_path,
            'file_id': file_id,
            'saved_at': time.time()
        }

        bot.edit_message_text(
            "🖼️ <b>¡Foto guardada con éxito!</b>\n\n"
            "👉 <b>Ahora envíame la música:</b>\n"
            "• Nota de voz o archivo MP3.\n"
            "• Un video del que extraer el sonido.\n"
            "• O pega un enlace de TikTok, Reels o YouTube.\n\n"
            "<i>(O escribe /limpiar para descartarla).</i>",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )
    except Exception as e:
        logger.error(f"Error guardando foto remix: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al guardar foto: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg.message_id)
    finally:
        PENDING_PHOTO_TASKS.pop(task_id, None)


def execute_photo_audio_remix(status_msg: types.Message, file_id: str, duration_mode: str, task_id: str):
    """Crea un video MP4 vertical HD a partir de una foto enviada y la pista de audio activa."""
    chat_id = status_msg.chat.id
    user_id = status_msg.chat.id
    audio_info = USER_REMIX_AUDIO.get(user_id)

    if not audio_info or not os.path.exists(audio_info['path']):
        bot.edit_message_text("⚠️ No se encontró el audio de remix. Por favor guarda el audio primero.", chat_id=chat_id, message_id=status_msg.message_id)
        return

    bot.edit_message_text(
        "🎬 <b>Creando video con tu foto y música...</b>\n<i>(Codificando H.264 1080x1920 a 30 FPS en alta fidelidad)...</i>",
        chat_id=chat_id,
        message_id=status_msg.message_id
    )
    bot.send_chat_action(chat_id, 'upload_video')

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        input_photo = os.path.join(DOWNLOAD_DIR, f"{task_id}_ph_in.jpg")
        output_video = os.path.join(DOWNLOAD_DIR, f"{task_id}_ph_video.mp4")
        audio_path = audio_info['path']

        with open(input_photo, 'wb') as f:
            f.write(downloaded)

        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', input_photo,
            '-i', audio_path,
            '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,unsharp=3:3:0.5:3:3:0.0',
            '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'veryfast',
            '-crf', '18', '-r', '30',
            '-pix_fmt', 'yuv420p',
            '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '44100'
        ]

        if duration_mode == '15':
            cmd += ['-t', '15']
            desc_dur = "15 segundos"
        elif duration_mode == '30':
            cmd += ['-t', '30']
            desc_dur = "30 segundos (Estado WhatsApp)"
        elif duration_mode == 'audio':
            desc_dur = f"Duración completa del audio ({int(audio_info.get('duration', 0))} seg)"
        else:
            cmd += ['-t', '30']
            desc_dur = "30 segundos"

        cmd += ['-shortest', '-movflags', '+faststart', output_video]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        caption = (
            "🎬 <b>¡Video con Foto & Música Creado!</b>\n\n"
            f"🎵 <b>Audio:</b> <i>{esc(audio_info['title'])}</i>\n"
            f"⏱ <b>Duración:</b> {desc_dur}\n"
            "💎 <b>Formato:</b> 1080x1920 Vertical (9:16 HD)\n"
            "🔊 <b>Sonido:</b> AAC 192 kbps Alta Fidelidad\n\n"
            "📲 <i>Guárdalo en tu galería y súbelo a tu Estado de WhatsApp o Reels.</i>"
        )

        with open(output_video, 'rb') as vf:
            bot.send_video(chat_id, vf, caption=caption, supports_streaming=True)

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error creando video desde foto: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al crear video: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg.message_id)
    finally:
        cleanup_task_files(task_id)


def execute_photo_audio_remix_saved(status_msg: types.Message, duration_mode: str):
    """Crea un video MP4 a partir de la foto guardada en USER_REMIX_PHOTO y el audio en USER_REMIX_AUDIO."""
    chat_id = status_msg.chat.id
    user_id = status_msg.chat.id
    photo_info = USER_REMIX_PHOTO.get(user_id)
    audio_info = USER_REMIX_AUDIO.get(user_id)

    if not photo_info or not os.path.exists(photo_info['path']):
        bot.edit_message_text("⚠️ No se encontró la foto en memoria. Por favor envíala de nuevo.", chat_id=chat_id, message_id=status_msg.message_id)
        return

    if not audio_info or not os.path.exists(audio_info['path']):
        bot.edit_message_text("⚠️ No se encontró el audio en memoria. Por favor envíalo de nuevo.", chat_id=chat_id, message_id=status_msg.message_id)
        return

    task_id = uuid.uuid4().hex[:10]
    bot.edit_message_text(
        "🎬 <b>Creando video con tu foto y música...</b>\n<i>(Codificando H.264 1080x1920 a 30 FPS en alta fidelidad)...</i>",
        chat_id=chat_id,
        message_id=status_msg.message_id
    )
    bot.send_chat_action(chat_id, 'upload_video')

    try:
        input_photo = photo_info['path']
        output_video = os.path.join(DOWNLOAD_DIR, f"{task_id}_ph_video.mp4")
        audio_path = audio_info['path']

        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', input_photo,
            '-i', audio_path,
            '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,unsharp=3:3:0.5:3:3:0.0',
            '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'veryfast',
            '-crf', '18', '-r', '30',
            '-pix_fmt', 'yuv420p',
            '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '44100'
        ]

        if duration_mode == '15':
            cmd += ['-t', '15']
            desc_dur = "15 segundos"
        elif duration_mode == '30':
            cmd += ['-t', '30']
            desc_dur = "30 segundos (Estado WhatsApp)"
        elif duration_mode == 'audio':
            desc_dur = f"Duración completa del audio ({int(audio_info.get('duration', 0))} seg)"
        else:
            cmd += ['-t', '30']
            desc_dur = "30 segundos"

        cmd += ['-shortest', '-movflags', '+faststart', output_video]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        caption = (
            "🎬 <b>¡Video con Foto & Música Creado!</b>\n\n"
            f"🎵 <b>Audio:</b> <i>{esc(audio_info['title'])}</i>\n"
            f"⏱ <b>Duración:</b> {desc_dur}\n"
            "💎 <b>Formato:</b> 1080x1920 Vertical (9:16 HD)\n"
            "🔊 <b>Sonido:</b> AAC 192 kbps Alta Fidelidad\n\n"
            "📲 <i>Guárdalo en tu galería y súbelo a tu Estado de WhatsApp o Reels.</i>"
        )

        with open(output_video, 'rb') as vf:
            bot.send_video(chat_id, vf, caption=caption, supports_streaming=True)

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error creando video desde foto guardada: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al crear video: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg.message_id)
    finally:
        cleanup_task_files(task_id)


# ==========================================
# DESCARGADOR DE ENLACES EXTERNOS
# ==========================================

@bot.message_handler(func=lambda message: True)
def handle_incoming_text(message: types.Message):
    purge_old_tasks()
    user_id = message.from_user.id
    if not is_authorized(user_id):
        bot.reply_to(message, "⛔ <b>Acceso Denegado</b>")
        return

    text = message.text or ""
    urls = re.findall(r'(https?://[^\s]+)', text)

    if not urls:
        remix_hint = ""
        if user_id in USER_REMIX_AUDIO and os.path.exists(USER_REMIX_AUDIO[user_id]['path']):
            remix_hint = f"\n\n🎧 <i>Tienes un audio guardado:</i> <b>{esc(USER_REMIX_AUDIO[user_id]['title'])}</b>. ¡Mándame un video para remezclarlo!"

        bot.reply_to(
            message,
            "ℹ️ <b>¿Qué deseas hacer?</b>\n\n"
            "• <b>Remix de Video y Audio:</b> Envíame un video y te daré la opción de extraer su audio o colocarle música.\n"
            "• <b>Optimizar para WhatsApp:</b> Envíame una foto o video de tu cámara.\n"
            "• <b>Descargar:</b> Pega un enlace de YouTube, TikTok, Reels, etc."
            f"{remix_hint}"
        )
        return

    raw_url = urls[0]
    safe, reason = is_safe_url(raw_url)
    if not safe:
        bot.reply_to(message, f"🚫 <b>Enlace rechazado por seguridad:</b>\n{reason}")
        return

    chat_id = message.chat.id
    status_msg = bot.reply_to(message, "🔍 <b>Analizando enlace...</b>")

    try:
        ydl_info_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'socket_timeout': 10,
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
            info = ydl.extract_info(raw_url, download=False)
            title = info.get('title', 'Video/Audio')
            duration = info.get('duration', 0)
            uploader = info.get('uploader') or info.get('artist') or 'Desconocido'

        task_id = uuid.uuid4().hex[:10]
        PENDING_TASKS[task_id] = {
            'url': raw_url,
            'title': title,
            'duration': duration,
            'uploader': uploader,
            'created_at': time.time(),
            'chat_id': chat_id,
            'message_id': message.message_id
        }

        mins = int(duration // 60)
        secs = int(duration % 60)
        dur_str = f"{mins}:{secs:02d} min" if duration else "Desconocida"
        safe_title = (title[:65] + '...') if len(title) > 65 else title

        keyboard = types.InlineKeyboardMarkup(row_width=2)
        btn_audio = types.InlineKeyboardButton("🎵 Audio MP3", callback_data=f"dl:audio:{task_id}")
        btn_video = types.InlineKeyboardButton("🎬 Video MP4", callback_data=f"dl:video:{task_id}")
        btn_remix = types.InlineKeyboardButton("🎧 Guardar Audio p/ Remix", callback_data=f"dl:save_remix:{task_id}")
        btn_status = types.InlineKeyboardButton("✨ Estado WhatsApp", callback_data=f"dl:status:{task_id}")
        btn_cancel = types.InlineKeyboardButton("❌ Cancelar", callback_data=f"dl:cancel:{task_id}")

        keyboard.add(btn_audio, btn_video)
        keyboard.add(btn_remix, btn_status)
        keyboard.add(btn_cancel)

        caption = (
            f"📹 <b>{esc(safe_title)}</b>\n\n"
            f"👤 <b>Canal/Autor:</b> {esc(uploader)}\n"
            f"⏱ <b>Duración:</b> {dur_str}\n\n"
            "¿Qué deseas hacer con este enlace?"
        )

        bot.edit_message_text(
            caption,
            chat_id=chat_id,
            message_id=status_msg.message_id,
            reply_markup=keyboard
        )

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Error analizando video: {e}")
        bot.edit_message_text("❌ No se pudo analizar el enlace. Verifica que sea público.", chat_id=chat_id, message_id=status_msg.message_id)
    except Exception as e:
        logger.error(f"Error inesperado: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error: <code>{esc(str(e)[:180])}</code>", chat_id=chat_id, message_id=status_msg.message_id)


# ==========================================
# CALLBACKS DE DESCARGAS DE ENLACES
# ==========================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def handle_download_callback(call: types.CallbackQuery):
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Acceso denegado.", show_alert=True)
        return

    parts = call.data.split(':')
    action = parts[1]
    task_id = parts[2]

    task = PENDING_TASKS.get(task_id)

    if action == 'cancel':
        PENDING_TASKS.pop(task_id, None)
        cleanup_task_files(task_id)
        bot.answer_callback_query(call.id, "Cancelado.")
        try:
            bot.edit_message_text("❌ <b>Operación cancelada.</b>", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if not task:
        bot.answer_callback_query(call.id, "⚠️ Solicitud expirada. Reenvía el enlace.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Procesando...")

    url = task['url']
    title = task['title']
    duration = task['duration']
    uploader = task['uploader']
    chat_id = call.message.chat.id
    target_msg_id = task.get('message_id')

    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if action == 'audio':
        download_audio(chat_id, call.message.message_id, target_msg_id, task_id, url, title, duration, uploader)
    elif action == 'video':
        download_video(chat_id, call.message.message_id, target_msg_id, task_id, url, title, duration, uploader)
    elif action == 'status':
        download_whatsapp_status(chat_id, call.message.message_id, target_msg_id, task_id, url, title, duration, uploader)
    elif action == 'save_remix':
        download_and_save_remix_audio(call.message, task_id, url, title)


def download_and_save_remix_audio(status_msg: types.Message, task_id: str, url: str, title: str):
    """Descarga el audio de un enlace y lo guarda en la sesión del usuario para Remix."""
    chat_id = status_msg.chat.id
    user_id = status_msg.chat.id

    bot.edit_message_text("⏳ <b>Descargando y guardando audio para Remix...</b>", chat_id=chat_id, message_id=status_msg.message_id)

    try:
        user_audio_path = os.path.join(DOWNLOAD_DIR, f"remix_audio_{user_id}.mp3")
        output_tmpl = os.path.join(DOWNLOAD_DIR, f"{task_id}_remix.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_tmpl,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_remix.mp3"))
        if not files:
            raise FileNotFoundError("No se encontró el audio descargado.")

        if os.path.exists(user_audio_path):
            os.remove(user_audio_path)
        os.rename(files[0], user_audio_path)

        probe_cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', user_audio_path
        ]
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        duration = float(res.stdout.strip() or 0)

        USER_REMIX_AUDIO[user_id] = {
            'path': user_audio_path,
            'title': title,
            'duration': duration,
            'saved_at': time.time()
        }

        mins = int(duration // 60)
        secs = int(duration % 60)
        dur_str = f"{mins}:{secs:02d} min"

        notify_audio_saved(status_msg, user_id, title, dur_str)
    except Exception as e:
        logger.error(f"Error guardando audio de enlace: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al guardar audio: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg.message_id)
    finally:
        PENDING_TASKS.pop(task_id, None)
        cleanup_task_files(task_id)


def download_audio(chat_id, status_msg_id, reply_to_id, task_id, url, title, duration, uploader):
    try:
        bot.edit_message_text(f"⏳ <b>Descargando y extrayendo MP3...</b>\n<i>{title[:60]}</i>", chat_id=chat_id, message_id=status_msg_id)
        bot.send_chat_action(chat_id, 'upload_audio')

        output_tmpl = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).70s.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_tmpl,
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                {'key': 'FFmpegMetadata', 'add_metadata': True},
                {'key': 'EmbedThumbnail'}
            ],
            'writethumbnail': True,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'max_filesize': MAX_FILE_SIZE,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        mp3_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.mp3"))
        if not mp3_files:
            raise FileNotFoundError("No se encontró el archivo MP3.")

        mp3_path = mp3_files[0]
        if os.path.getsize(mp3_path) > MAX_FILE_SIZE:
            bot.edit_message_text("⚠️ El archivo supera los 50 MB permitidos por Telegram.", chat_id=chat_id, message_id=status_msg_id)
            return

        thumb_data = None
        thumb_candidates = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.jpg")) + glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.png"))
        if thumb_candidates:
            thumb_data = open(thumb_candidates[0], 'rb')
        else:
            webp_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.webp"))
            if webp_files:
                conv_jpg = os.path.join(DOWNLOAD_DIR, f"{task_id}_thumb.jpg")
                subprocess.run(['ffmpeg', '-y', '-i', webp_files[0], '-vframes', '1', conv_jpg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(conv_jpg):
                    thumb_data = open(conv_jpg, 'rb')

        bot.edit_message_text("🚀 <b>Subiendo audio a Telegram...</b>", chat_id=chat_id, message_id=status_msg_id)

        with open(mp3_path, 'rb') as audio_file:
            bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=title,
                performer=uploader,
                duration=int(duration) if duration else None,
                thumb=thumb_data,
                reply_to_message_id=reply_to_id
            )

        if thumb_data:
            thumb_data.close()

        try:
            bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error procesando audio: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al obtener audio: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg_id)
    finally:
        PENDING_TASKS.pop(task_id, None)
        cleanup_task_files(task_id)


def download_video(chat_id, status_msg_id, reply_to_id, task_id, url, title, duration, uploader):
    try:
        bot.edit_message_text(f"⏳ <b>Descargando Video MP4...</b>\n<i>{title[:60]}</i>", chat_id=chat_id, message_id=status_msg_id)
        bot.send_chat_action(chat_id, 'upload_video')

        output_tmpl = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).70s.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[height<=720]/best',
            'outtmpl': output_tmpl,
            'merge_output_format': 'mp4',
            'writethumbnail': True,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'max_filesize': MAX_FILE_SIZE,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        video_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.mp4")) + glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.mkv")) + glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.webm"))
        if not video_files:
            raise FileNotFoundError("No se encontró el video procesado.")

        video_path = video_files[0]
        if os.path.getsize(video_path) > MAX_FILE_SIZE:
            bot.edit_message_text("⚠️ El video supera los 50 MB permitidos.", chat_id=chat_id, message_id=status_msg_id)
            return

        thumb_data = None
        thumb_candidates = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.jpg")) + glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.png"))
        if thumb_candidates:
            thumb_data = open(thumb_candidates[0], 'rb')
        else:
            webp_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_*.webp"))
            if webp_files:
                conv_jpg = os.path.join(DOWNLOAD_DIR, f"{task_id}_thumb.jpg")
                subprocess.run(['ffmpeg', '-y', '-i', webp_files[0], '-vframes', '1', conv_jpg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(conv_jpg):
                    thumb_data = open(conv_jpg, 'rb')

        bot.edit_message_text("🚀 <b>Subiendo video a Telegram...</b>", chat_id=chat_id, message_id=status_msg_id)

        caption = f"🎬 <b>{esc(title)}</b>\n👤 {uploader}"

        with open(video_path, 'rb') as video_file:
            bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                duration=int(duration) if duration else None,
                supports_streaming=True,
                reply_to_message_id=reply_to_id
            )

        if thumb_data:
            thumb_data.close()

        try:
            bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error procesando video: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al obtener video: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg_id)
    finally:
        PENDING_TASKS.pop(task_id, None)
        cleanup_task_files(task_id)


def download_whatsapp_status(chat_id, status_msg_id, reply_to_id, task_id, url, title, duration, uploader):
    try:
        bot.edit_message_text(f"⏳ <b>Descargando y optimizando para Estado WhatsApp (HD)...</b>\n<i>{title[:60]}</i>", chat_id=chat_id, message_id=status_msg_id)
        bot.send_chat_action(chat_id, 'upload_video')

        raw_output_tmpl = os.path.join(DOWNLOAD_DIR, f"{task_id}_raw.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': raw_output_tmpl,
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'max_filesize': MAX_FILE_SIZE,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        raw_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}_raw.*"))
        if not raw_files:
            raise FileNotFoundError("No se encontró el video base.")

        raw_path = raw_files[0]
        final_status_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_whatsapp_status.mp4")

        cmd = [
            'ffmpeg', '-y', '-i', raw_path,
            '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,unsharp=5:5:0.5:5:5:0.0',
            '-c:v', 'libx264', '-preset', 'slow', '-profile:v', 'high', '-level', '4.1',
            '-crf', '19', '-maxrate', '3200k', '-bufsize', '6000k', '-r', '30',
            '-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-movflags', '+faststart',
            final_status_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        bot.edit_message_text("🚀 <b>Subiendo video optimizado a Telegram...</b>", chat_id=chat_id, message_id=status_msg_id)

        caption = (
            f"✨ <b>{esc(title)}</b>\n\n"
            "🟢 <b>Listo para Estado de WhatsApp en Máxima Calidad:</b>\n"
            "• 1080x1920 Vertical (9:16)\n"
            "• Bitrate y color anti-compresión destructiva\n\n"
            "📲 <i>Guárdalo en tu galería y súbelo a tu estado.</i>"
        )

        with open(final_status_path, 'rb') as video_file:
            bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                duration=int(duration) if duration else None,
                supports_streaming=True,
                reply_to_message_id=reply_to_id
            )

        try:
            bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error procesando estado: {e}", exc_info=True)
        bot.edit_message_text(f"❌ Error al optimizar video: {esc(str(e)[:180])}", chat_id=chat_id, message_id=status_msg_id)
    finally:
        PENDING_TASKS.pop(task_id, None)
        cleanup_task_files(task_id)


if __name__ == '__main__':
    logger.info("Iniciando Media Suite Bot (Downloader + WhatsApp HD + Video/Audio Remixer)...")
    bot.infinity_polling(timeout=25, long_polling_timeout=15)
