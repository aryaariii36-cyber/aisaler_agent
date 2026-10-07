import os

try:  # optional: load a local .env file (ignored on PaaS where real env vars are set)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return val


# --- Telegram -----------------------------------------------------------
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
# Public HTTPS URL Liara gives your app, e.g. https://your-app.liara.run
WEBHOOK_BASE_URL = _require("WEBHOOK_BASE_URL")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/telegram/webhook")
# Shared secret Telegram sends back in a header on every webhook call,
# so random people can't POST fake updates to your endpoint.
WEBHOOK_SECRET = _require("WEBHOOK_SECRET_TOKEN")
PORT = int(os.environ.get("PORT", "8080"))

# Admin chat to receive low-confidence payment slips / store notifications.
ADMIN_CHAT_ID = int(_require("ADMIN_CHAT_ID"))

# --- LLM provider (GapGPT - OpenAI-compatible endpoint) -----------------
# GapGPT عملاً یک پروکسی OpenAI-compatible هست، پس با کلاینت OpenAI SDK کار می‌کنه.
GAPGPT_API_KEY = _require("GAPGPT_API_KEY")
GAPGPT_BASE_URL = os.environ.get("GAPGPT_BASE_URL", "https://api.gapgpt.app/v1")
GAPGPT_MODEL = os.environ.get("GAPGPT_MODEL", "gpt-4o-mini")

# --- PostgreSQL -------------------------------------------------------
DB_HOST = _require("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = _require("DB_NAME")
DB_USER = _require("DB_USER")
DB_PASSWORD = _require("DB_PASSWORD")

# اندازه‌ی connection pool. با معماری جدید (جدول مشترک به‌جای per-user
# schema) هزینه‌ی هر کوئری خیلی سبک‌تر شده، ولی وقتی چند نمونه از بات
# هم‌زمان (مقیاس افقی) بالا میان، هر نمونه pool مستقل خودش رو داره؛ پس این
# مقدار رو متناسب با max_connections دیتابیس و تعداد نمونه‌ها در PaaS تنظیم
# کن (مثلاً اگر ۳ نمونه داری و DB اجازه‌ی ۶۰ اتصال می‌ده، هر نمونه max_size≈20).
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "20"))

# --- Store payment destination (receipts are verified against these) -------
STORE_CARD_NUMBER = _require("STORE_CARD_NUMBER")
STORE_CARD_HOLDER = _require("STORE_CARD_HOLDER")
STORE_BANK_NAME = _require("STORE_BANK_NAME")

STORE_NAME = os.environ.get("STORE_NAME", "فروشگاه پوشاک")

# --- Inactivity reminder timer ------------------------------------------
# چند ثانیه بعد از آخرین پیام کاربر (بدون اینکه ادامه بده) بات یک یادآوری
# بفرسته. این مقدار رو در تنظیمات PaaS (متغیرهای محیطی) ست کن؛ اگر ست
# نشه، پیش‌فرض ۳۶۰۰ ثانیه (۱ ساعت) استفاده میشه. واحد: ثانیه.
REMINDER_DELAY_SECONDS = int(os.environ.get("REMINDER_DELAY_SECONDS", "3600"))

# هر چند ثانیه یک‌بار background worker موعدرسیده‌های shopbot.pending_reminders
# رو چک/ارسال کنه. عدد کوچیک‌تر = دقت زمان‌بندی بالاتر ولی بار بیشتر روی DB؛
# روی چند نمونه‌ی هم‌زمان از بات هم امن است (به handlers.py نگاه کن).
REMINDER_POLL_INTERVAL_SECONDS = int(os.environ.get("REMINDER_POLL_INTERVAL_SECONDS", "15"))

# --- کش کاتالوگ (برای رفع N+1 و کاهش هزینه/تاخیر هر پیام) ----------------
# متن خلاصه‌ی کاتالوگ که به LLM داده می‌شه، تا این مدت (ثانیه) در حافظه‌ی
# هر پروسه کش می‌شه؛ چون کاتالوگ خیلی کمتر از پیام‌های کاربر تغییر می‌کنه.
CATALOG_CACHE_TTL_SECONDS = int(os.environ.get("CATALOG_CACHE_TTL_SECONDS", "30"))

# --- فاکتور / تحویل ------------------------------------------------------
# بازه‌ی تقریبی تحویل (روز کاری) که در فاکتور نهایی نمایش داده میشه.
DELIVERY_MIN_DAYS = int(os.environ.get("DELIVERY_MIN_DAYS", "2"))
DELIVERY_MAX_DAYS = int(os.environ.get("DELIVERY_MAX_DAYS", "4"))
