import logging
import os
import re
import subprocess
import time
from datetime import datetime
from typing import Optional, Tuple

import phonenumbers
import pytz
import requests
from phonenumbers import geocoder

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


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

    return (
        "🎙️ <b>Voice Recording Received</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{flag} <b>Country:</b> <code>{country_name}</code>\n"
        f"📞 <b>Number:</b> <code>{masked_phone}</code>\n"
        f"🚦 <b>Termination:</b> <code>{termination}</code>\n"
        f"⏰ <b>Time:</b> <code>{date_str}</code> | <code>{time_str} {period}</code>\n"
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


def send_instant_notification_sync(call_info: dict) -> Optional[int]:
    try:
        message = build_instant_notification(call_info)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, data=data, timeout=10)
        result = response.json()

        if result.get('ok'):
            return result['result']['message_id']
        return None
    except Exception as e:
        logger.error(f"Notification error: {e}")
        return None


def send_to_telegram_sync(audio_file: str, call_info: dict, notification_msg_id: Optional[int] = None) -> bool:
    try:
        caption = build_caption(call_info)
        _ensure_file_ready(audio_file)

        ogg_file = convert_to_ogg_opus(audio_file)
        _ensure_file_ready(ogg_file)

        duration_num = _probe_duration(ogg_file or audio_file)

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
        with open(ogg_file, 'rb') as audio:
            files = {'voice': audio}
            data = {
                'chat_id': TELEGRAM_CHAT_ID,
                'caption': caption,
                'parse_mode': 'HTML',
                'duration': duration_num or None,
            }
            response = requests.post(url, files=files, data=data, timeout=180)

        if not response.ok:
            logger.error(
                "Telegram sendVoice failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )

        if notification_msg_id:
            try:
                del_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
                requests.post(
                    del_url,
                    data={'chat_id': TELEGRAM_CHAT_ID, 'message_id': notification_msg_id},
                    timeout=10,
                )
            except Exception:
                pass

        for candidate in {audio_file, ogg_file}:
            if candidate and os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except Exception:
                    pass

        return response.json().get('ok', False)
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False
