# Telegram Media Suite Bot

English · [Español](#español)

A 24/7 Telegram bot (Python, `pyTelegramBotAPI` + `yt-dlp` + `ffmpeg`) that downloads audio/video from links, optimizes photos/videos for WhatsApp Status without recompression artifacts, and remixes audio over video using stream copy (no quality loss).

## Features
- Audio (MP3) / video (MP4) download from external links.
- WhatsApp Status optimizer for photos and video (H.264 HD, 30 FPS).
- Video + audio **remixer** with `ffmpeg` stream copy and custom duration.
- **Security-first**:
  - Fail-closed user allow-list: an empty `ALLOWED_USER_IDS` rejects everyone.
  - SSRF protection: scheme check, blocked hostnames, and validation of **every** resolved A/AAAA address.
  - Untrusted titles, uploader names and error messages are HTML-escaped before being sent.
  - File-size limits and secrets only via environment variables.
- Runs as a **sandboxed systemd service** ([`deploy/telegram-downloader.service`](deploy/telegram-downloader.service)): dedicated user, `ProtectSystem=strict`, `NoNewPrivileges`, memory/CPU caps.

## Run
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS
python bot.py
```
Requires `ffmpeg` on the host. Paths default to the script directory (`.env`, `downloads/`); override with `ENV_FILE` and `DOWNLOAD_DIR`. For production install the unit file and `systemctl enable --now telegram-downloader`.

## Tests
```bash
pip install -r requirements-dev.txt
pytest
```
Covers the allow-list (including fail-closed), SSRF blocking (schemes, loopback, metadata endpoints, private or mixed DNS answers) and HTML escaping. No network or real token required.

## Known limitations
- `yt-dlp` re-resolves hostnames and follows redirects, so DNS-rebinding/redirect SSRF is mitigated but not eliminated. Run with restricted egress.
- Session state (pending tasks, saved audio) lives in memory and is lost on restart.

---

## Español

Bot de Telegram 24/7 en Python que descarga audio/video desde enlaces, optimiza fotos y videos para Estados de WhatsApp sin compresión adicional y remezcla audio sobre video con *stream copy* (sin pérdida de calidad).

**Seguridad:** lista blanca *fail-closed* (sin `ALLOWED_USER_IDS` nadie entra), protección SSRF (se validan todas las IP resueltas), escape HTML de datos externos, límite de tamaño, secretos solo por variables de entorno y ejecución como servicio systemd aislado (usuario dedicado, sistema de archivos de solo lectura, límites de CPU/RAM).

**Uso:** crea un `venv`, instala `requirements.txt`, copia `.env.example` a `.env` con tu token y ejecuta `python bot.py`. Requiere `ffmpeg`. Pruebas: `pip install -r requirements-dev.txt && pytest`.

**Limitaciones:** `yt-dlp` vuelve a resolver el DNS y sigue redirecciones, por lo que el SSRF se mitiga pero no se elimina del todo; conviene restringir la salida de red del servicio.
