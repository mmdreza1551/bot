import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

import phonenumbers
import pytz
from phonenumbers import geocoder
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio

from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELETHON_SESSION,
)

logger = logging.getLogger(__name__)

_telethon_client = None
_client_lock = threading.Lock()


def country_code_to_flag(country_code: Optional[str]) -> str:
    if not country_code or len(country_code) != 2:
        return '🌍'
    country_code = country_code.upper()
    return ''.join(chr(0x1F1E6 + ord(char) - ord('A')) for char in country_code)


def get_country_flag_and_name(phone_number: str) -> Tuple[str, str]:
    try:
        clean_number = re.sub(r'[^\d+]', '', str(phone_number))
        if not clean_number.startswith('+'):
            clean_number = '+' + clean_number
        try:
            parsed = phonenumbers.parse(clean_number, None)
            from phonenumbers import region_code_for_number

            country_iso = region_code_for_number(parsed)
            country_name = geocoder.description_for_number(parsed, "en")
            flag = country_code_to_flag(country_iso) if country_iso else '🌍'
            if not country_name:
                country_name = (
                    f"{country_iso} +{parsed.country_code}"
                    if country_iso
                    else f"+{parsed.country_code}"
                )
            return flag, country_name
        except Exception:
            match = re.match(r'\+?(\d{1,4})', clean_number)
            if match:
                code = match.group(1)
                return '🌍', f"Country Code +{code}"
        return '🌍', "Unknown"
    except Exception:
        return '🌍', "Unknown"


def mask_phone_number(phone_display: str) -> str:
    try:
        parsed = phonenumbers.parse(phone_display, None)
        country_code = f"+{parsed.country_code}"
        national_number = str(parsed.national_number)
        masked_national = (
            '*' * (len(national_number) - 3) + national_number[-3:]
            if len(national_number) > 3
            else national_number
        )
        return country_code + masked_national
    except Exception:
        return (
            phone_display[:4] + '******' + phone_display[-3:]
            if len(phone_display) > 7
            else phone_display
        )


def build_caption(call_info: dict) -> str:
    flag, country_name = get_country_flag_and_name(call_info.get('did', ''))
    phone_display = (
        call_info.get('did')
        if str(call_info.get('did', '')).startswith('+')
        else f"+{call_info.get('did', '')}"
    )
    masked_phone = mask_phone_number(phone_display)

    bd_timezone = pytz.timezone('Asia/Dhaka')
    bd_time = datetime.now(bd_timezone)
    date_str = bd_time.strftime('%m/%d/%Y')
    time_str = bd_time.strftime('%I:%M:%S')
    period = bd_time.strftime('%p')

    termination = call_info.get('termination', 'Unknown')
    cli = call_info.get('cli', 'Unknown')
    duration = call_info.get('duration', '—')
    revenue = call_info.get('revenue', '—')

    return (
        "🎙️ <b>Voice Recording Received</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{flag} <b>Country:</b> <code>{country_name}</code>\n"
        f"📞 <b>Number:</b> <code>{masked_phone}</code>\n"
        f"👤 <b>CLI:</b> <code>{cli}</code>\n"
        f"🚦 <b>Termination:</b> <code>{termination}</code>\n"
        f"⏱️ <b>Duration:</b> <code>{duration}</code>\n"
        f"💰 <b>Revenue:</b> <code>{revenue}</code>\n"
        f"🕒 <b>Logged at:</b> <code>{date_str}</code> | <code>{time_str} {period}</code>\n"
        "🏁 <b>Termination</b> ✅"
    )


def build_instant_notification(call_info: dict) -> str:
    flag, _ = get_country_flag_and_name(call_info.get('did', ''))
    phone_display = (
        call_info.get('did')
        if str(call_info.get('did', '')).startswith('+')
        else f"+{call_info.get('did', '')}"
    )
    masked_phone = mask_phone_number(phone_display)
    termination = call_info.get('termination', 'Unknown')

    return (
        "<b>📞 New call received</b>\n\n"
        f"{flag} <code>{masked_phone}</code>\n"
        f"🚦 <b>Termination:</b> <code>{termination}</code>"
    )


def broadcast_admins_sync(message: str, admin_ids) -> None:
    try:
        client = _get_client()
        with _client_lock:
            for admin_id in admin_ids:
                client.send_message(int(admin_id), f"🔔 <b>Admin Notification</b>\n\n{message}", parse_mode='html')
    except Exception as e:
        logger.error(f"Admin broadcast failed: {e}")


def convert_to_ogg_opus(input_file: str) -> str:
    base, _ = os.path.splitext(input_file)
    ogg_file = base + ".ogg"

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_file,
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                "-vn",
                ogg_file,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return ogg_file
    except Exception as e:
        logger.error(f"FFmpeg ogg/opus convert error: {e}")
        return input_file


def pad_audio_tail(input_file: str, pad_seconds: int = 2) -> str:
    """Add a small silent tail to prevent Telegram truncation."""

    try:
        duration = _probe_duration(input_file)
        if duration <= 0:
            return input_file

        padded_file = os.path.splitext(input_file)[0] + "_padded.ogg"
        target_duration = duration + pad_seconds

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_file,
                "-af",
                f"apad=pad_dur={pad_seconds}",
                "-t",
                str(target_duration),
                "-c:a",
                "libopus",
                padded_file,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

        return padded_file if os.path.exists(padded_file) else input_file
    except Exception as e:
        logger.error(f"Tail padding error: {e}")
        return input_file


def _probe_duration(file_path: str) -> int:
    try:
        result = subprocess.run(
            [
                'ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration',
                '-of',
                'default=noprint_wrappers=1:nokey=1',
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            duration_str = result.stdout.strip()
            if duration_str:
                return int(float(duration_str))
    except Exception:
        pass
    return 0


def _ensure_file_ready(file_path: str, retries: int = 3, delay: int = 2) -> None:
    for attempt in range(retries):
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return
        time.sleep(delay * (attempt + 1))


def _get_client() -> TelegramClient:
    global _telethon_client
    if _telethon_client:
        return _telethon_client

    with _client_lock:
        if _telethon_client:
            return _telethon_client

        client = TelegramClient(TELETHON_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        client.start(bot_token=TELEGRAM_BOT_TOKEN)
        _telethon_client = client
        logger.info("🤖 Telethon client ready for Telegram sending")
        return _telethon_client


def send_instant_notification_sync(call_info: dict) -> Optional[int]:
    try:
        message = build_instant_notification(call_info)
        client = _get_client()

        with _client_lock:
            sent = client.send_message(int(TELEGRAM_CHAT_ID), message, parse_mode='html')

        return sent.id if sent else None
    except Exception as e:
        logger.error(f"Notification error: {e}")
        return None


def send_to_telegram_sync(audio_file: str, call_info: dict, notification_msg_id: Optional[int] = None) -> bool:
    try:
        caption = build_caption(call_info)
        _ensure_file_ready(audio_file)

        ogg_file = convert_to_ogg_opus(audio_file)
        _ensure_file_ready(ogg_file)

        ogg_file = pad_audio_tail(ogg_file)
        _ensure_file_ready(ogg_file)

        duration_num = _probe_duration(ogg_file or audio_file)

        client = _get_client()
        attributes = []
        if duration_num:
            attributes.append(DocumentAttributeAudio(duration=int(duration_num), voice=True))

        with _client_lock:
            sent = client.send_file(
                int(TELEGRAM_CHAT_ID),
                ogg_file,
                caption=caption,
                voice_note=True,
                attributes=attributes,
                parse_mode='html',
            )

            if notification_msg_id:
                try:
                    client.delete_messages(int(TELEGRAM_CHAT_ID), ids=notification_msg_id)
                except Exception:
                    pass

        for candidate in {audio_file, ogg_file}:
            if candidate and os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except Exception:
                    pass

        return bool(sent)
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False
