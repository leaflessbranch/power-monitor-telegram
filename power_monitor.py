#!/usr/bin/env python3
import os
import sys
import time
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest

def load_device_config():
    """Load device configuration from file"""
    config_file = os.environ.get("POWER_MONITOR_DEVICES", "/etc/power-monitor/devices.json")
    default_devices = []

    try:
        with open(config_file, 'r') as f:
            data = json.load(f)
            return data.get("monitored_devices", default_devices)
    except Exception as e:
        # Called at import time, before logging is configured, so use stderr.
        print(f"WARNING: Could not load device config from {config_file}: {e}",
              file=sys.stderr)
        return default_devices


# Configuration
CONFIG = {
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    "telegram_base_url": os.environ.get("TELEGRAM_BASE_URL", "https://api.telegram.org/bot"),
    "telegram_base_file_url": os.environ.get("TELEGRAM_BASE_FILE_URL", "https://api.telegram.org/file/bot"),
    "monitored_devices": load_device_config(),
    "check_interval": 30,  # seconds
    "ping_timeout": 5,     # seconds
    "ping_count": 5,       # number of pings per check
    "db_path": os.environ.get("POWER_MONITOR_DB", "/var/lib/power_monitor/power_cuts.db"),
    "log_path": os.environ.get("POWER_MONITOR_LOG", "/var/log/power_monitor.log"),
    # Machine-readable state export for automated consumers (agents, scripts).
    # Written atomically after every check. Separate from the human Telegram alert.
    "state_file": os.environ.get("POWER_MONITOR_STATE_FILE", "/run/power-monitor/state.json"),
    # An outage is only reported as "confirmed" once it has lasted this long.
    # Consumers that take disruptive action should ignore unconfirmed outages.
    "confirm_after": int(os.environ.get("POWER_MONITOR_CONFIRM_AFTER", "600")),  # seconds
    # Optional read-only HTTP endpoint serving the same state, for consumers on
    # other machines. Disabled unless a port is set. Stdlib only, no new deps.
    # Bind to a LAN address or 0.0.0.0; the payload carries nothing sensitive,
    # but there is no auth, so do not expose it to the internet.
    "http_port": int(os.environ.get("POWER_MONITOR_HTTP_PORT", "0")),
    "http_bind": os.environ.get("POWER_MONITOR_HTTP_BIND", "0.0.0.0"),
}

# Validate required environment variables
if not CONFIG["telegram_bot_token"]:
    print("ERROR: TELEGRAM_BOT_TOKEN environment variable is not set!")
    sys.exit(1)

if not CONFIG["telegram_chat_id"]:
    print("ERROR: TELEGRAM_CHAT_ID environment variable is not set!")
    sys.exit(1)

if not CONFIG["monitored_devices"]:
    print("ERROR: No monitored devices configured!")
    print("Add them to /etc/power-monitor/devices.json (see examples/devices.json.example).")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["log_path"]),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO. Telegram API URLs embed the bot token,
# so INFO-level httpx logging writes the token in cleartext to the log file and
# the journal on every poll. Keep it at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class PowerMonitor:
    def __init__(self):
        self.db_path = CONFIG["db_path"]
        self.bot = Bot(
            token=CONFIG["telegram_bot_token"],
            base_url=CONFIG["telegram_base_url"],
            base_file_url=CONFIG["telegram_base_file_url"],
        )
        self.chat_id = CONFIG["telegram_chat_id"]
        self.current_status = "UNKNOWN"
        self.last_outage_start = None
        self.latest_state = None
        self.init_database()
        self.handle_startup_recovery()

    def init_database(self):
        """Initialize SQLite database for storing power cut history"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS power_cuts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                status TEXT DEFAULT 'ongoing'
            )
        ''')
        conn.commit()
        conn.close()
        
    def handle_startup_recovery(self):
        """Handle recovery from unexpected shutdowns"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Check for any ongoing power cuts
        cursor.execute(
            """SELECT id, start_time FROM power_cuts 
               WHERE status = 'ongoing' 
               ORDER BY id DESC"""
        )
        ongoing_cuts = cursor.fetchall()
        
        if ongoing_cuts:
            logger.info(f"Found {len(ongoing_cuts)} ongoing power cuts at startup")
            
            for cut_id, start_time in ongoing_cuts:
                # Mark them as completed with recovery note
                cursor.execute(
                    """UPDATE power_cuts 
                       SET end_time = datetime('now'), 
                           duration_seconds = CAST((julianday(datetime('now')) - julianday(start_time)) * 86400 AS INTEGER),
                           status = 'completed'
                       WHERE id = ?""",
                    (cut_id,)
                )
                
            conn.commit()
            logger.info("Closed all ongoing power cuts due to unexpected shutdown")
            
        conn.close()

    def ping_device(self, ip: str) -> bool:
        """Ping a device and return True if reachable"""
        try:
            result = subprocess.run(
                ["ping", "-c", str(CONFIG["ping_count"]), "-W", str(CONFIG["ping_timeout"]), ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error pinging {ip}: {e}")
            return False

    def check_power_status(self) -> bool:
        """Check if power is available by pinging monitored devices"""
        reachable_count = 0
        for device in CONFIG["monitored_devices"]:
            if self.ping_device(device["ip"]):
                reachable_count += 1
                logger.debug(f"{device['name']} ({device['ip']}) is reachable")
            else:
                logger.debug(f"{device['name']} ({device['ip']}) is NOT reachable")

        # Power is considered ON if at least one device is reachable
        # Power is considered OFF if ALL devices are unreachable
        return reachable_count > 0

    async def send_telegram_message(self, message: str, include_keyboard: bool = True):
        """Send a message via Telegram"""
        try:
            keyboard = None
            if include_keyboard:
                keyboard = [
                    [
                        InlineKeyboardButton("📊 Status", callback_data="status"),
                        InlineKeyboardButton("📈 History", callback_data="history")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
            else:
                reply_markup = None
                
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            logger.info(f"Telegram message sent: {message}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def record_power_cut_start(self):
        """Record the start of a power cut"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute(
            "INSERT INTO power_cuts (start_time) VALUES (?)",
            (now,)
        )
        conn.commit()
        cut_id = cursor.lastrowid
        conn.close()
        self.last_outage_start = now
        return cut_id, now

    def record_power_cut_end(self):
        """Record the end of a power cut"""
        if not self.last_outage_start:
            return None, None, None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now()
        duration = (now - self.last_outage_start).total_seconds()

        # NOTE: ORDER BY/LIMIT on UPDATE requires SQLite built with
        # SQLITE_ENABLE_UPDATE_DELETE_LIMIT. Debian's build enables it, but
        # stock CPython and musl builds do not, where it is a syntax error.
        # The subquery form is portable everywhere.
        cursor.execute(
            """UPDATE power_cuts
               SET end_time = ?, duration_seconds = ?, status = 'completed'
               WHERE id = (SELECT id FROM power_cuts
                           WHERE status = 'ongoing'
                           ORDER BY id DESC LIMIT 1)""",
            (now, int(duration))
        )
        conn.commit()
        conn.close()

        return now, duration

    def build_state(self, power_on: bool) -> Dict:
        """Build the machine-readable power state payload.

        Deliberately separate from the human Telegram alert: consumers get a
        stable schema with no prose. Shared by the state file and the optional
        HTTP endpoint so both always agree.

        Schema (v1):
          schema           int     format version; refuse to act on unknown ones
          state            str     "up" | "down" (device reachability)
          since            str     ISO8601 start of current outage, null when up
          elapsed_seconds  int     time in current outage, 0 when up
          confirmed        bool    outage has lasted >= confirm_after
          checked_at       str     ISO8601 of this check; detects a wedged writer
        """
        now = datetime.now()

        if power_on or not self.last_outage_start:
            return {
                "schema": 1,
                "state": "up",
                "since": None,
                "elapsed_seconds": 0,
                "confirmed": False,
                "checked_at": now.isoformat(),
            }

        elapsed = (now - self.last_outage_start).total_seconds()
        return {
            "schema": 1,
            "state": "down",
            "since": self.last_outage_start.isoformat(),
            "elapsed_seconds": int(elapsed),
            "confirmed": elapsed >= CONFIG["confirm_after"],
            "checked_at": now.isoformat(),
        }

    def write_state_file(self, power_on: bool):
        """Write the state payload atomically, so readers never see a partial file.

        Also caches it for the HTTP endpoint, which serves the last computed
        state rather than re-deriving it.
        """
        state = self.build_state(power_on)
        self.latest_state = state
        path = CONFIG["state_file"]

        if not path:
            return

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except Exception as e:
            # Never let state export break monitoring; the Telegram path is
            # the safety-critical one.
            logger.error(f"Could not write state file {path}: {e}")

    def get_current_status(self) -> Dict:
        """Get current power status and ongoing outage info"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, start_time FROM power_cuts
               WHERE status = 'ongoing'
               ORDER BY id DESC LIMIT 1"""
        )
        ongoing = cursor.fetchone()
        conn.close()

        if ongoing:
            start_time = datetime.fromisoformat(ongoing[1])
            duration = (datetime.now() - start_time).total_seconds()
            return {
                "status": "POWER_CUT",
                "outage_start": start_time,
                "duration_seconds": int(duration)
            }
        else:
            return {"status": "POWER_ON"}

    def get_power_cut_history(self, days: int = 30) -> List[Dict]:
        """Get power cut history for the last N days"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        since_date = datetime.now() - timedelta(days=days)

        cursor.execute(
            """SELECT start_time, end_time, duration_seconds, status
               FROM power_cuts
               WHERE start_time > ?
               ORDER BY start_time DESC""",
            (since_date,)
        )

        cuts = []
        for row in cursor.fetchall():
            cuts.append({
                "start_time": row[0],
                "end_time": row[1],
                "duration_seconds": row[2],
                "status": row[3]
            })

        conn.close()
        return cuts

    async def monitor_loop(self):
        """Main monitoring loop"""
        logger.info("Starting power monitoring...")
        await self.send_telegram_message("🔌 Power monitoring system started")

        while True:
            try:
                power_on = self.check_power_status()

                if power_on and self.current_status == "POWER_CUT":
                    # Power restored
                    end_time, duration = self.record_power_cut_end()
                    if end_time:
                        duration_str = self.format_duration(duration)
                        message = (
                            f"✅ *Power Restored!*\n"
                            f"📅 Time: {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"⏱️ Outage Duration: {duration_str}"
                        )
                        await self.send_telegram_message(message)
                    self.current_status = "POWER_ON"

                elif not power_on and self.current_status != "POWER_CUT":
                    # Power cut detected
                    cut_id, start_time = self.record_power_cut_start()
                    message = (
                        f"🚨 *Power Cut Detected!*\n"
                        f"📅 Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"🔋 Server running on UPS backup"
                    )
                    await self.send_telegram_message(message)
                    self.current_status = "POWER_CUT"

                elif self.current_status == "UNKNOWN":
                    # Initial status
                    self.current_status = "POWER_ON" if power_on else "POWER_CUT"
                    if self.current_status == "POWER_CUT":
                        self.record_power_cut_start()

                self.write_state_file(power_on)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")

            await asyncio.sleep(CONFIG["check_interval"])

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format duration in a readable format"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if secs > 0 or not parts:
            parts.append(f"{secs}s")

        return " ".join(parts)

class TelegramBot:
    def __init__(self, monitor: PowerMonitor):
        self.monitor = monitor
        self.application = (
            Application.builder()
            .token(CONFIG["telegram_bot_token"])
            .base_url(CONFIG["telegram_base_url"])
            .base_file_url(CONFIG["telegram_base_file_url"])
            .build()
        )

        # Add command handlers
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("history", self.cmd_history))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("fix", self.cmd_fix))
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        status = self.monitor.get_current_status()

        if status["status"] == "POWER_CUT":
            duration_str = self.monitor.format_duration(status["duration_seconds"])
            message = (
                f"🔴 *Current Status: POWER CUT*\n"
                f"📅 Started: {status['outage_start'].strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"⏱️ Duration: {duration_str} (ongoing)\n"
                f"🔋 Server is running on UPS backup"
            )
        else:
            message = "🟢 *Current Status: POWER ON*\n✅ All systems normal"

        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=self.get_keyboard())

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /history command"""
        await self.show_history_page(update, context, page=0)

    async def show_history_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, is_callback: bool = False):
        """Show paginated history"""
        cuts = self.monitor.get_power_cut_history(30)
        items_per_page = 10
        total_pages = (len(cuts) + items_per_page - 1) // items_per_page if cuts else 1
        
        # Ensure page is within bounds
        page = max(0, min(page, total_pages - 1))
        
        if not cuts:
            message = "📊 *Power Cut History (Last 30 Days)*\n\nNo power cuts recorded."
            keyboard = self.get_keyboard()
        else:
            start_idx = page * items_per_page
            end_idx = min(start_idx + items_per_page, len(cuts))
            page_cuts = cuts[start_idx:end_idx]
            
            message = f"📊 *Power Cut History (Last 30 Days)*\n"
            message += f"📄 Page {page + 1} of {total_pages}\n\n"

            for cut in page_cuts:
                start_time = datetime.fromisoformat(cut["start_time"])
                status_icon = "🔴" if cut["status"] == "ongoing" else "✅"

                message += f"{status_icon} *{start_time.strftime('%Y-%m-%d %H:%M')}*"

                if cut["duration_seconds"]:
                    duration_str = self.monitor.format_duration(cut["duration_seconds"])
                    message += f" - Duration: {duration_str}"
                else:
                    message += " - Ongoing"

                message += "\n"

            # Add statistics
            total_cuts = len([c for c in cuts if c["status"] == "completed"])
            total_duration = sum(c["duration_seconds"] or 0 for c in cuts if c["duration_seconds"])
            avg_duration = total_duration / total_cuts if total_cuts > 0 else 0

            message += (
                f"\n📈 *Statistics:*\n"
                f"Total Cuts: {total_cuts}\n"
                f"Total Downtime: {self.monitor.format_duration(total_duration)}\n"
                f"Average Duration: {self.monitor.format_duration(avg_duration)}"
            )
            
            # Create pagination keyboard
            keyboard = self.get_history_keyboard(page, total_pages)

        if is_callback:
            # This is a callback query, edit the existing message
            try:
                await update.callback_query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    # Message content is identical, just acknowledge the callback
                    logger.debug("Message content unchanged, skipping edit")
                else:
                    # Re-raise other BadRequest errors
                    raise
        else:
            # This is a regular command, send new message
            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        message = (
            "🤖 *Power Monitor Bot Commands*\n\n"
            "/status - Check current power status\n"
            "/history - View power cut history (last 30 days)\n"
            "/fix - Fix stuck states (use if status is incorrect)\n"
            "/help - Show this help message\n\n"
            "The bot will automatically notify you when:\n"
            "• A power cut is detected\n"
            "• Power is restored"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=self.get_keyboard())
        
    async def cmd_fix(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /fix command to reset stuck states"""
        # Close any ongoing power cuts
        conn = sqlite3.connect(self.monitor.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE power_cuts 
               SET end_time = datetime('now'), 
                   status = 'completed',
                   duration_seconds = CAST((julianday(datetime('now')) - julianday(start_time)) * 86400 AS INTEGER)
               WHERE status = 'ongoing'"""
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        # Reset monitor status
        self.monitor.current_status = "UNKNOWN"
        self.monitor.last_outage_start = None
        
        message = (
            f"🔧 *Fix Applied*\n"
            f"Closed {affected} ongoing power cut(s)\n"
            f"Reset monitor status\n"
            f"The system will re-check power status shortly."
        )
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=self.get_keyboard())

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        message = (
            "👋 *Welcome to Power Monitor Bot!*\n\n"
            "I'll monitor your power status and notify you of any outages.\n"
            "Use the buttons below to interact with me:"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=self.get_keyboard())

    def get_keyboard(self):
        """Create inline keyboard with command buttons"""
        keyboard = [
            [
                InlineKeyboardButton("📊 Status", callback_data="status"),
                InlineKeyboardButton("📈 History", callback_data="history")
            ],
            [
                InlineKeyboardButton("🔧 Fix", callback_data="fix"),
                InlineKeyboardButton("❓ Help", callback_data="help")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_history_keyboard(self, current_page: int, total_pages: int):
        """Create pagination keyboard for history"""
        keyboard = []
        
        # Pagination row
        pagination_row = []
        if current_page > 0:
            pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"history_page_{current_page - 1}"))
        if current_page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"history_page_{current_page + 1}"))
        
        if pagination_row:
            keyboard.append(pagination_row)
        
        # Main command buttons
        keyboard.extend([
            [
                InlineKeyboardButton("📊 Status", callback_data="status"),
                InlineKeyboardButton("🔄 Refresh", callback_data="history")
            ],
            [
                InlineKeyboardButton("🔧 Fix", callback_data="fix"),
                InlineKeyboardButton("❓ Help", callback_data="help")
            ]
        ])
        
        return InlineKeyboardMarkup(keyboard)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button presses"""
        query = update.callback_query
        await query.answer()
        
        # Handle pagination callbacks
        if query.data.startswith("history_page_"):
            page = int(query.data.split("_")[-1])
            await self.show_history_page(update, context, page=page, is_callback=True)
        # Map callback data to command methods
        elif query.data == "status":
            # Create a fake update with the query message for command reuse
            fake_update = Update(
                update_id=update.update_id,
                message=query.message
            )
            await self.cmd_status(fake_update, context)
        elif query.data == "history":
            await self.show_history_page(update, context, page=0, is_callback=True)
        elif query.data == "help":
            # Create a fake update with the query message for command reuse
            fake_update = Update(
                update_id=update.update_id,
                message=query.message
            )
            await self.cmd_help(fake_update, context)
        elif query.data == "fix":
            # Create a fake update with the query message for command reuse
            fake_update = Update(
                update_id=update.update_id,
                message=query.message
            )
            await self.cmd_fix(fake_update, context)

    async def run(self):
        """Run the Telegram bot"""
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

class StateHTTPServer(ThreadingHTTPServer):
    """Serves the monitor's latest state to consumers on other machines."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, monitor):
        self.monitor = monitor
        super().__init__(addr, StateHTTPHandler)


class StateHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/state", "/agent/state"):
            self.send_error(404)
            return

        state = self.server.monitor.latest_state
        if state is None:
            # No check has completed yet. 503 rather than a guess, so consumers
            # fall back to "unknown" instead of inferring an outage.
            body = json.dumps({"schema": 1, "state": "unknown",
                               "reason": "no check completed yet"}).encode()
            self.send_response(503)
        else:
            body = json.dumps(state).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.debug("state http: " + fmt % args)


def start_http_server(monitor):
    """Start the state endpoint if configured. Never fatal: the Telegram path
    is safety-critical and must survive a port conflict here."""
    port = CONFIG["http_port"]
    if not port:
        return None
    try:
        server = StateHTTPServer((CONFIG["http_bind"], port), monitor)
    except Exception as e:
        logger.error(f"Could not start state HTTP server on "
                     f"{CONFIG['http_bind']}:{port}: {e}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"State endpoint listening on {CONFIG['http_bind']}:{port}")
    return server


async def main():
    """Main function"""
    monitor = PowerMonitor()
    bot = TelegramBot(monitor)

    start_http_server(monitor)

    # Run both the monitor and the bot concurrently
    await asyncio.gather(
        monitor.monitor_loop(),
        bot.run()
    )

if __name__ == "__main__":
    asyncio.run(main())