import sys

try:
    import selenium
    import requests
    import pytz
    from telegram import Bot
    from webdriver_manager.chrome import ChromeDriverManager
    from PIL import Image
except ImportError as e:
    print(f"ERROR: Missing required package: {e}")
    print("Please run: pip install -r requirements.txt pillow")
    sys.exit(1)

import os
import time
import logging
import requests
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode
import subprocess
from datetime import datetime
import pytz
from concurrent.futures import ThreadPoolExecutor
import threading
import json
import shlex
from PIL import Image

from config import (
    ADMIN_IDS,
    BACKGROUND_IMAGE,
    DEFAULT_SETTINGS,
    LOGIN_EMAIL,
    LOGIN_PASSWORD,
    ORANGECARRIER_CALLS_URL,
    ORANGECARRIER_LOGIN_URL,
    SETTINGS_FILE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    VOICE_MODE,
)
from messaging import send_instant_notification_sync, send_to_telegram_sync

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global state
bot_settings = DEFAULT_SETTINGS.copy()
processed_calls = set()
driver_instance = None
is_monitoring = False
telegram_app = None

# ==================== Settings Manager ====================

def load_settings():
    """بارگذاری تنظیمات از فایل"""
    global bot_settings
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                persisted = json.load(f)
                bot_settings = {**DEFAULT_SETTINGS, **persisted}
            logger.info("✅ Settings loaded successfully")
        else:
            save_settings()
    except Exception as e:
        logger.error(f"Error loading settings: {e}")
        bot_settings = DEFAULT_SETTINGS.copy()

def save_settings():
    """ذخیره تنظیمات در فایل"""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(bot_settings, f, indent=2)
        logger.info("✅ Settings saved successfully")
    except Exception as e:
        logger.error(f"Error saving settings: {e}")

# ==================== Utility Functions ====================

def ffprobe_duration(path):
    try:
        out = subprocess.check_output(
            shlex.split(f'ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "{path}"'),
            stderr=subprocess.STDOUT, timeout=20
        ).decode().strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0

def wait_size_stable(session, url, headers, stable_checks=5, max_wait=90):
    last = None
    same = 0
    waited = 0
    while waited < max_wait:
        try:
            r = session.head(url, headers={**headers, "Cache-Control": "no-cache"},
                             allow_redirects=True, timeout=10)
            if r.status_code in (200, 206):
                size = r.headers.get("Content-Length")
                size = int(size) if size and size.isdigit() else None
                if size and size == last:
                    same += 1
                    if same >= stable_checks:
                        return size
                else:
                    same = 0
                last = size
        except Exception:
            pass
        time.sleep(1)
        waited += 1
    return last

def get_image_dimensions(image_path):
    """دریافت ابعاد تصویر"""
    try:
        with Image.open(image_path) as img:
            return img.size
    except Exception as e:
        logger.error(f"Error getting image dimensions: {e}")
        return (320, 320)

# ==================== Admin Notification System ====================

def notify_admins_sync(message: str):
    """ارسال نوتیفیکیشن به تمام ادمین‌ها (سینک)"""
    for admin_id in ADMIN_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                'chat_id': admin_id,
                'text': f"🔔 <b>Admin Notification</b>\n\n{message}",
                'parse_mode': 'HTML'
            }
            requests.post(url, data=data, timeout=10)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

def notify_connection_lost(reason: str = "Unknown"):
    """اطلاع قطع اتصال به ادمین‌ها"""
    message = (
        "⚠️ <b>Connection Lost</b>\n\n"
        f"Reason: {reason}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "System will retry automatically..."
    )
    notify_admins_sync(message)

def notify_connection_restored():
    """اطلاع برقراری مجدد اتصال"""
    message = (
        "✅ <b>Connection Restored</b>\n\n"
        "Monitoring resumed successfully\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    notify_admins_sync(message)

# ==================== Driver Setup ====================

def setup_driver():
    """Setup Chrome driver"""
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

    import shutil
    chrome_binary = os.environ.get('CHROME_BINARY')
    if chrome_binary and os.path.exists(chrome_binary):
        chrome_options.binary_location = chrome_binary
    else:
        for binary_name in ['chromium', 'chromium-browser', 'google-chrome', 'chrome']:
            binary_path = shutil.which(binary_name)
            if binary_path:
                chrome_options.binary_location = binary_path
                break

    prefs = {
        'profile.default_content_setting_values.media_stream_mic': 1,
        'profile.default_content_setting_values.media_stream_camera': 1,
        'profile.default_content_setting_values.notifications': 1
    }
    chrome_options.add_experimental_option('prefs', prefs)
    chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL', 'browser': 'ALL'})

    try:
        chromedriver_path = os.environ.get('CHROMEDRIVER_PATH')
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(chromedriver_path)
        else:
            import shutil as _shutil
            chromedriver_in_path = _shutil.which('chromedriver')
            if chromedriver_in_path:
                service = Service(chromedriver_in_path)
            else:
                service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)

    driver.execute_cdp_cmd('Network.enable', {})
    return driver

def login_to_orangecarrier(driver, max_retries: int = 3) -> bool:
    """Login with retry"""
    for attempt in range(max_retries):
        try:
            logger.info(f"Login attempt {attempt + 1}/{max_retries}...")
            driver.get(ORANGECARRIER_LOGIN_URL)
            time.sleep(3)

            email_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "email"))
            )
            email_field.clear()
            email_field.send_keys(LOGIN_EMAIL)
            time.sleep(0.5)
            password_field = driver.find_element(By.NAME, "password")
            password_field.clear()
            password_field.send_keys(LOGIN_PASSWORD)
            time.sleep(1)
            login_button = driver.find_element(By.XPATH, "//button[@type='submit']")
            login_button.click()
            time.sleep(5)

            if "login" not in driver.current_url.lower():
                logger.info("✅ Login successful!")
                notify_connection_restored()
                return True

        except Exception as e:
            logger.error(f"Login attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                notify_connection_lost(f"Login failed after {max_retries} attempts")
            time.sleep(bot_settings.get('retry_delay', 30))

    return False

# ==================== Call Processing ====================
# ⚠️ این تابع از نسخه قدیمی که درست کار می‌کرد آورده شده
#     با تمام fallback ها و لاگ‌ها، بدون تغییر در پنل ادمین

def get_active_calls(driver):
    """Extract active calls from the page - robust version with fallbacks"""
    try:
        # Save page source for debugging (optional، اگر نمی‌خوای هر بار بنویسه می‌تونی کامنت کنی)
        try:
            with open('page_debug.html', 'w', encoding='utf-8') as f:
                f.write(driver.page_source)
        except Exception:
            pass

        # Wait for the table to load
        time.sleep(2)

        calls = []

        # Method 1: Look for table with class "table" that has active calls
        # Structure: Termination | DID | CLI | Duration | Revenue | [Play Button]
        try:
            tables = driver.find_elements(By.CSS_SELECTOR, "table.table")

            for table in tables:
                tbody = table.find_element(By.TAG_NAME, "tbody")
                rows = tbody.find_elements(By.TAG_NAME, "tr")

                logger.info(f"Found {len(rows)} row(s) in table")

                for row in rows:
                    try:
                        cells = row.find_elements(By.TAG_NAME, "td")

                        if len(cells) >= 5:  # Termination, DID, CLI, Duration, Revenue
                            termination = cells[0].text.strip()
                            did = cells[1].text.strip()
                            cli = cells[2].text.strip()
                            duration = cells[3].text.strip()
                            revenue = cells[4].text.strip()

                            play_button = None
                            uuid = None

                            try:
                                # Try finding button in the row
                                play_button = row.find_element(By.CSS_SELECTOR, "button[class*='btn']")
                            except Exception:
                                try:
                                    # Try alternative selectors
                                    play_button = row.find_element(By.XPATH, ".//button")
                                except Exception:
                                    try:
                                        # Look for any clickable element with play-ish attributes
                                        play_button = row.find_element(
                                            By.XPATH,
                                            ".//*[contains(@class, 'play') or contains(@onclick, 'play')]"
                                        )
                                    except Exception:
                                        logger.debug("No play button found for row")
                                        continue

                            # Extract UUID from play button attributes - REQUIRED for API method!
                            if play_button:
                                try:
                                    uuid = None

                                    # Try onclick attribute first (most reliable)
                                    onclick = play_button.get_attribute('onclick')
                                    if onclick:
                                        # Extract UUID - multiple patterns
                                        # Pattern 1: playCall('1761406796.3808732') or playCall("1761406796.3808732")
                                        uuid_match = re.search(r"playCall\(['\"](\d+\.\d+)['\"]\)", onclick)
                                        if uuid_match:
                                            uuid = uuid_match.group(1)
                                        else:
                                            # Pattern 2: any number.number format in quotes
                                            uuid_match = re.search(r"['\"](\d{10,}\.\d+)['\"]", onclick)
                                            if uuid_match:
                                                uuid = uuid_match.group(1)

                                        if uuid:
                                            logger.info(f"✓ Extracted UUID from onclick: {uuid}")

                                    # Fallback: Try all possible attributes
                                    if not uuid:
                                        for attr in ['data-uuid', 'data-call-id', 'data-id', 'id']:
                                            candidate = play_button.get_attribute(attr)
                                            if candidate and re.match(r'^\d{10,}\.\d+$', candidate):
                                                uuid = candidate
                                                logger.info(f"✓ Extracted UUID from {attr}: {uuid}")
                                                break

                                    # Try extracting from button's parent row attributes
                                    if not uuid:
                                        try:
                                            parent_row = play_button.find_element(By.XPATH, "./ancestor::tr[1]")
                                            for attr in ['data-uuid', 'data-call-id', 'data-id']:
                                                candidate = parent_row.get_attribute(attr)
                                                if candidate and re.match(r'^\d{10,}\.\d+$', candidate):
                                                    uuid = candidate
                                                    logger.info(f"✓ Extracted UUID from row {attr}: {uuid}")
                                                    break
                                        except Exception:
                                            pass

                                    # Validate UUID format (should be like: 1234567890.12345)
                                    if uuid:
                                        if not re.match(r'^\d{10,}\.\d+$', uuid):
                                            logger.warning(f"⚠ Invalid UUID format '{uuid}' for call {did} - skipping")
                                            continue
                                        logger.info(f"✅ Valid UUID extracted: {uuid}")
                                    else:
                                        logger.warning(
                                            f"⚠ Could not extract UUID for call {did} - skipping (API requires UUID)"
                                        )
                                        try:
                                            button_html = play_button.get_attribute('outerHTML')
                                            logger.debug(f"Button HTML: {button_html[:200]}")
                                        except Exception:
                                            pass
                                        continue  # Skip this call if no UUID found
                                except Exception as e:
                                    logger.warning(f"⚠ UUID extraction error for {did}: {e} - skipping")
                                    continue  # Skip this call on error

                            # Create unique identifier using termination, did, cli
                            call_id = f"{termination}_{did}_{cli}"

                            # Check if already processed and validate data
                            if call_id not in processed_calls and did and cli:
                                logger.info(
                                    f"Found call: Termination={termination}, DID={did}, CLI={cli}, "
                                    f"Duration={duration}, Revenue={revenue}, UUID={uuid or 'N/A'}"
                                )

                                calls.append({
                                    'id': call_id,
                                    'termination': termination,
                                    'did': did,
                                    'cli': cli,
                                    'duration': duration,
                                    'revenue': revenue,
                                    'uuid': uuid,
                                    'play_button': play_button,
                                    'row': row
                                })

                    except Exception as e:
                        logger.debug(f"Error processing row: {e}")
                        continue

        except Exception as e:
            logger.debug(f"Table method 1 failed: {e}")

        # Method 2: If no calls found, try direct play button search (fallback)
        if not calls:
            logger.info("Trying fallback method to find play buttons...")
            play_buttons = driver.find_elements(By.XPATH, "//button[contains(@class, 'btn')]")

            logger.info(f"Found {len(play_buttons)} button(s)")

            for button in play_buttons:
                try:
                    # Get the parent row (tr element)
                    row = button
                    for _ in range(5):  # Try up to 5 levels up
                        row = row.find_element(By.XPATH, "..")
                        if row.tag_name.lower() == 'tr':
                            break

                    cells = row.find_elements(By.TAG_NAME, "td")

                    if len(cells) >= 5:
                        termination = cells[0].text.strip()
                        did = cells[1].text.strip()
                        cli = cells[2].text.strip()
                        duration = cells[3].text.strip()
                        revenue = cells[4].text.strip()

                        uuid = None
                        try:
                            onclick = button.get_attribute('onclick')
                            if onclick:
                                # Pattern 1: playCall('1761406796.3808732')
                                uuid_match = re.search(r"playCall\(['\"](\d+\.\d+)['\"]\)", onclick)
                                if uuid_match:
                                    uuid = uuid_match.group(1)
                                else:
                                    # Pattern 2: any long number.number format
                                    uuid_match = re.search(r"['\"](\d{10,}\.\d+)['\"]", onclick)
                                    if uuid_match:
                                        uuid = uuid_match.group(1)

                                if uuid:
                                    logger.info(f"✓ Extracted UUID from onclick (fallback): {uuid}")

                            if not uuid:
                                for attr in ['data-uuid', 'data-call-id', 'data-id', 'id']:
                                    candidate = button.get_attribute(attr)
                                    if candidate and re.match(r'^\d{10,}\.\d+$', candidate):
                                        uuid = candidate
                                        logger.info(f"✓ Extracted UUID from {attr} (fallback): {uuid}")
                                        break

                            if uuid:
                                if not re.match(r'^\d{10,}\.\d+$', uuid):
                                    logger.warning(
                                        f"⚠ Invalid UUID format '{uuid}' for call {did} (fallback) - skipping"
                                    )
                                    continue
                                logger.info(f"✅ Valid UUID extracted (fallback): {uuid}")
                            else:
                                logger.warning(
                                    f"⚠ Could not extract UUID for call {did} (fallback) - skipping"
                                )
                                try:
                                    button_html = button.get_attribute('outerHTML')
                                    logger.debug(f"Button HTML: {button_html[:200]}")
                                except Exception:
                                    pass
                                continue
                        except Exception as e:
                            logger.warning(
                                f"⚠ UUID extraction error (fallback) for {did}: {e} - skipping"
                            )
                            continue

                        call_id = f"{termination}_{did}_{cli}"

                        if call_id not in processed_calls and did and cli:
                            logger.info(
                                f"Found call (fallback): Termination={termination}, DID={did}, "
                                f"CLI={cli}, Duration={duration}, UUID={uuid or 'N/A'}"
                            )

                            calls.append({
                                'id': call_id,
                                'termination': termination,
                                'did': did,
                                'cli': cli,
                                'duration': duration,
                                'revenue': revenue,
                                'uuid': uuid,
                                'play_button': button,
                                'row': row
                            })

                except Exception as e:
                    logger.debug(f"Fallback row error: {e}")
                    continue

        return calls

    except Exception as e:
        logger.error(f"Error getting calls: {e}")
        return []

def download_audio_via_api(session_cookies, did, uuid, call_id, wait_for_completion=True):
    """Download audio via API"""
    try:
        api_url = f"https://www.orangecarrier.com/live/calls/sound?did={did}&uuid={uuid}"

        session = requests.Session()
        for cookie in session_cookies:
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain'))

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Referer': 'https://www.orangecarrier.com/live/calls',
            'Accept': '*/*',
            'Accept-Encoding': 'identity',
            'Cache-Control': 'no-cache',
        }

        if wait_for_completion:
            logger.info(f"⏳ [{call_id}] Waiting for recording...")
            wait_size_stable(session, api_url + f"&_ts={int(time.time())}", headers,
                             stable_checks=5, max_wait=90)

        r = session.get(api_url + f"&_ts={int(time.time())}", headers=headers, timeout=180)
        r.raise_for_status()

        if 'audio' not in r.headers.get('Content-Type', ''):
            return None

        ctype = r.headers.get('Content-Type', '')
        ext = 'wav' if 'wav' in ctype else 'mp3'

        filename = f"call_{call_id}.{ext}"
        with open(filename, 'wb') as f:
            f.write(r.content)

        target = 6.5
        for attempt in range(3):
            dur = ffprobe_duration(filename)
            if dur >= target:
                return filename
            time.sleep(3 * (attempt + 1))
            r = session.get(api_url + f"&_ts={int(time.time())}", headers=headers, timeout=180)
            r.raise_for_status()
            with open(filename, 'wb') as f:
                f.write(r.content)

        return filename
    except Exception as e:
        logger.error(f"❌ [{call_id}] Download failed: {e}")
        return None

def process_single_call(session_cookies, call, notification_msg_id=None):
    """Process single call"""
    call_id = call['id']
    try:
        logger.info(f"🚀 [{call_id}] Processing...")

        if call.get('uuid') and call.get('did'):
            audio_file = download_audio_via_api(
                session_cookies,
                call['did'],
                call['uuid'],
                call_id,
                wait_for_completion=True
            )

            if audio_file:
                success = send_to_telegram_sync(audio_file, call, notification_msg_id)
                if success:
                    logger.info(f"✅ [{call_id}] Forwarded!")
                    return True
        return False
    except Exception as e:
        logger.error(f"❌ [{call_id}] Error: {e}")
        return False

# ==================== Monitoring ====================

def monitor_calls_with_recovery():
    """Monitor calls with auto-recovery"""
    global is_monitoring, driver_instance

    logger.info("🚀 Starting monitoring...")

    consecutive_errors = 0
    max_errors = 5

    while is_monitoring:
        try:
            try:
                driver_instance.get(ORANGECARRIER_CALLS_URL)
                time.sleep(3)

                if "login" in driver_instance.current_url.lower():
                    logger.warning("⚠️ Session expired, reconnecting...")
                    notify_connection_lost("Session expired")
                    if not login_to_orangecarrier(driver_instance):
                        consecutive_errors += 1
                        if consecutive_errors >= max_errors:
                            logger.error("❌ Max errors reached")
                            notify_connection_lost(f"Max errors ({max_errors})")
                            break
                        time.sleep(bot_settings.get('retry_delay', 30))
                        continue
                    consecutive_errors = 0
            except Exception as e:
                logger.error(f"Connection check failed: {e}")
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    break
                time.sleep(bot_settings.get('retry_delay', 30))
                continue

            session_cookies = driver_instance.get_cookies()
            calls = get_active_calls(driver_instance)

            if calls:
                new_calls = [call for call in calls if call['id'] not in processed_calls]

                if new_calls:
                    logger.info(f"🔥 Found {len(new_calls)} NEW call(s)")

                    executor = ThreadPoolExecutor(max_workers=500)

                    for call in new_calls:
                        processed_calls.add(call['id'])

                        notification_msg_id = send_instant_notification_sync(call)
                        executor.submit(process_single_call, session_cookies, call, notification_msg_id)

                    executor.shutdown(wait=False)

            consecutive_errors = 0
            time.sleep(2)

        except KeyboardInterrupt:
            is_monitoring = False
            break
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                notify_connection_lost(f"Too many errors: {e}")
                break
            time.sleep(10)

    logger.info("Monitoring stopped")

# ==================== Telegram Bot Handlers (Admin Panel) ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start"""
    user_id = str(update.effective_user.id)

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Access Denied")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("🖼️ Change Background", callback_data="change_bg")],
        [InlineKeyboardButton("📈 Statistics", callback_data="stats")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🎛️ <b>OrangeCarrier Admin Panel</b>\n\n"
        "Welcome to admin control panel!\n\n"
        "📱 <b>Quick Info:</b>\n"
        f"• Mode: <code>{VOICE_MODE.upper()}</code>\n"
        f"• Background: <code>{'Custom' if bot_settings.get('has_background') else 'Black'}</code>\n"
        f"• Status: <code>{'🟢 Active' if is_monitoring else '🔴 Stopped'}</code>"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show status"""
    query = update.callback_query
    await query.answer()

    text = (
        "📊 <b>System Status</b>\n\n"
        f"🔄 Monitoring: <code>{'🟢 Active' if is_monitoring else '🔴 Stopped'}</code>\n"
        f"📤 Send Mode: <code>{VOICE_MODE.upper()}</code>\n"
        f"🖼️ Background: <code>{'Custom' if bot_settings.get('has_background') else 'Black'}</code>\n"
        f"📏 Dimensions: <code>{bot_settings['background_dimensions']['width']}x"
        f"{bot_settings['background_dimensions']['height']}</code>\n"
        f"📞 Processed: <code>{len(processed_calls)}</code>\n"
        f"⏰ Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
    )

    keyboard = [[InlineKeyboardButton("« Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings"""
    query = update.callback_query
    await query.answer()

    text = (
        "⚙️ <b>Settings Menu</b>\n\n"
        f"Mode: <b>{VOICE_MODE.upper()}</b>\n"
        f"Background: <b>{'Custom' if bot_settings.get('has_background') else 'Default'}</b>"
    )

    keyboard = [
        [InlineKeyboardButton("🖼️ Change Background", callback_data="change_bg")],
        [InlineKeyboardButton("🗑️ Remove Background", callback_data="remove_bg")],
        [InlineKeyboardButton("♻️ Reset Settings", callback_data="reset_settings")],
        [InlineKeyboardButton("« Back", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def change_background_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Request background"""
    query = update.callback_query
    await query.answer()

    text = (
        "🖼️ <b>Upload Background Image</b>\n\n"
        "Send me an image to use as background.\n\n"
        "📝 Tips:\n"
        "• Any size supported\n"
        "• Video will match image size\n"
        "• JPG/PNG supported\n\n"
        "Send the image now..."
    )

    keyboard = [[InlineKeyboardButton("« Cancel", callback_data="settings")]]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_background_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded image"""
    user_id = str(update.effective_user.id)

    if user_id not in ADMIN_IDS:
        return

    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        await photo_file.download_to_drive(BACKGROUND_IMAGE)

        width, height = get_image_dimensions(BACKGROUND_IMAGE)

        bot_settings['has_background'] = True
        bot_settings['background_dimensions'] = {'width': width, 'height': height}
        save_settings()

        await update.message.reply_text(
            f"✅ <b>Background Updated!</b>\n\n"
            f"📏 Dimensions: <code>{width}x{height}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def remove_background_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove background"""
    query = update.callback_query

    if os.path.exists(BACKGROUND_IMAGE):
        os.remove(BACKGROUND_IMAGE)

    bot_settings['has_background'] = False
    bot_settings['background_dimensions'] = {'width': 320, 'height': 320}
    save_settings()

    await query.answer("✅ Background removed", show_alert=True)
    await settings_handler(update, context)

async def reset_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset settings"""
    query = update.callback_query

    global bot_settings
    bot_settings = DEFAULT_SETTINGS.copy()
    save_settings()

    if os.path.exists(BACKGROUND_IMAGE):
        os.remove(BACKGROUND_IMAGE)

    await query.answer("✅ Settings reset", show_alert=True)
    await settings_handler(update, context)

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show stats"""
    query = update.callback_query
    await query.answer()

    text = (
        "📈 <b>Statistics</b>\n\n"
        f"📞 Total Calls: <code>{len(processed_calls)}</code>\n"
        f"💾 Settings: <code>{'✅' if os.path.exists(SETTINGS_FILE) else '❌'}</code>\n"
        f"🖼️ Background: <code>{'✅' if os.path.exists(BACKGROUND_IMAGE) else '❌'}</code>"
    )

    keyboard = [[InlineKeyboardButton("« Back", callback_data="back_to_main")]]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def back_to_main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Back to main"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("🖼️ Change Background", callback_data="change_bg")],
        [InlineKeyboardButton("📈 Statistics", callback_data="stats")]
    ]

    text = (
        "🎛️ <b>OrangeCarrier Admin Panel</b>\n\n"
        "Welcome to admin control panel!\n\n"
        "📱 <b>Quick Info:</b>\n"
        f"• Mode: <code>{VOICE_MODE.upper()}</code>\n"
        f"• Background: <code>{'Custom' if bot_settings.get('has_background') else 'Black'}</code>\n"
        f"• Status: <code>{'🟢 Active' if is_monitoring else '🔴 Stopped'}</code>"
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ==================== Main & Bootstrap ====================

def start_monitoring_thread():
    """Start monitoring thread"""
    global driver_instance, is_monitoring

    if is_monitoring:
        logger.warning("Already monitoring")
        return

    try:
        driver_instance = setup_driver()
        if not login_to_orangecarrier(driver_instance):
            logger.error("Login failed")
            return

        is_monitoring = True
        monitoring_thread = threading.Thread(
            target=monitor_calls_with_recovery,
            daemon=True
        )
        monitoring_thread.start()
        logger.info("✅ Monitoring started")
    except Exception as e:
        logger.error(f"Failed to start: {e}")
        notify_connection_lost(f"Startup failed: {e}")

def main():
    """Main function"""
    logger.info("=" * 50)
    logger.info("OrangeCarrier Advanced Bot Starting...")
    logger.info("=" * 50)

    # Load settings
    load_settings()

    # Start monitoring in background
    start_monitoring_thread()

    # Setup Telegram bot (admin panel)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(status_handler, pattern="^status$"))
    app.add_handler(CallbackQueryHandler(settings_handler, pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(change_background_handler, pattern="^change_bg$"))
    app.add_handler(CallbackQueryHandler(remove_background_handler, pattern="^remove_bg$"))
    app.add_handler(CallbackQueryHandler(reset_settings_handler, pattern="^reset_settings$"))
    app.add_handler(CallbackQueryHandler(stats_handler, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(back_to_main_handler, pattern="^back_to_main$"))

    # فقط ادمین‌ها اجازه آپلود بک‌گراند دارند
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.User(user_id=[int(i) for i in ADMIN_IDS]),
            handle_background_image
        )
    )

    logger.info("🤖 Telegram bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
        is_monitoring = False
        if driver_instance:
            try:
                driver_instance.quit()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        is_monitoring = False
        if driver_instance:
            try:
                driver_instance.quit()
            except Exception:
                pass
