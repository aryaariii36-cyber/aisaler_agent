"""
تمام دسترسی به PostgreSQL از اینجا رد میشه.

معماری (جدول مشترک با chat_id ایندکس‌شده)
-------------------------------------------
همه‌ی داده‌ها (session/پیام‌ها/سفارش‌ها) در جدول‌های مشترکِ schema
``shopbot`` نگه‌داری می‌شن؛ هر ردیف با ستون ``chat_id`` (که روش ایندکس
گذاشته شده) به کاربرش وصل میشه. یعنی به‌جای ساختن یک PostgreSQL schema
جدا برای هر کاربر (که با رشد تعداد کاربر باعث تورم pg_catalog، سنگین شدن
autovacuum/planning و از بین رفتن plan cache می‌شد)، فقط یک‌بار در ابتدای
برنامه چند جدول ثابت ساخته می‌شه و مقیاس با تعداد کاربر فقط از طریق ایندکس
روی chat_id مدیریت می‌شه. اگر لازم شد، جدول‌های messages/orders بعداً
می‌تونن به‌سادگی روی chat_id partition (HASH یا RANGE روی created_at) بشن،
بدون اینکه لایه‌ی بالای کد (بقیه‌ی این فایل) تغییری نیاز داشته باشه.

از asyncpg با یک connection pool استفاده می‌کنیم تا برای هر پیام یک
اتصال جدید باز نشه. اندازه‌ی pool از env var قابل تنظیمه (به config.py
نگاه کن) تا وقتی چند نمونه از بات (horizontal scaling) پشت یک PaaS بالا
میاد، هر نمونه بشه متناسب با ظرفیت DB تنظیم شد.
"""

from typing import Any, Optional
from datetime import datetime

import asyncpg

from . import config

# نکته: ستون‌های تاریخ/ساعت اینجا TIMESTAMPTZ هستن و سرور PostgreSQL روی
# فرانکفورته. asyncpg مقدار TIMESTAMPTZ رو همیشه UTC-aware برمی‌گردونه (به
# GUC ای "timezone" روی connection پول کاری نداره)، پس تبدیل به وقت تهران
# *برای نمایش* باید توی پایتون و با app.timeutils.to_tehran انجام بشه، نه
# اینجا. مقادیر خام (UTC) همین‌جوری که هستن برای مقایسه/محاسبه خوبن.

_pool: Optional[asyncpg.Pool] = None

# چون هر ردیف کاربر توی یک جدول ثابت (نه schema جدا) زندگی می‌کنه، دیگه
# نیازی به ensure/DDL جدا به‌ازای هر کاربر نیست. فقط برای اینکه UPSERT
# سبک شمارنده‌ی last_seen_at سر هر پیام دوباره نوشته نشه، یک کش کوچیک در
# حافظه‌ی پروسه نگه می‌داریم (صرفاً یک بهینه‌سازی؛ گم شدنش بعد از ری‌استارت
# یا نبودنش روی نمونه‌ی دیگه در scale-out هیچ اثری روی درستی کار نداره).
_seen_recently: dict[int, datetime] = {}
_SEEN_CACHE_TTL_SECONDS = 300


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        min_size=config.DB_POOL_MIN_SIZE,
        max_size=config.DB_POOL_MAX_SIZE,
        server_settings={
            "search_path": "shopbot,public",
        },
    )
    await _ensure_schema()


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised - call init_pool() first.")
    return _pool


# --------------------------------------------------------------------------
# Schema: کاتالوگ محصولات + جدول‌های مشترک کاربر/session/پیام/سفارش
# --------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS shopbot;

-- دسته‌بندی محصولات (شلوار، پیراهن، تیشرت و ...). sort_order برای کنترل
-- ترتیب نمایش دکمه‌های دسته‌بندی به کاربره.
CREATE TABLE IF NOT EXISTS shopbot.categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shopbot.products (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    base_price   NUMERIC(12, 0) NOT NULL,
    image_url    TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- دسته‌بندی محصول (مثلاً شلوار/پیراهن/تیشرت).
    category_id  INTEGER REFERENCES shopbot.categories(id),
    -- جنسیت: 'مردانه' / 'زنانه' / یا هر مقدار دیگه‌ای که لازم باشه
    -- (مثلاً 'بچگانه'). اگر محصول جنسیتی نداره (یونیسکس)، NULL بمونه.
    gender       TEXT
);

-- برای دیتابیس‌هایی که از قبل جدول products رو بدون این ستون‌ها ساخته بودن.
ALTER TABLE shopbot.products ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES shopbot.categories(id);
ALTER TABLE shopbot.products ADD COLUMN IF NOT EXISTS gender TEXT;
CREATE INDEX IF NOT EXISTS idx_shopbot_products_category ON shopbot.products (category_id);

-- self-healing: محصولاتی که از قبل (قبل از اضافه شدن دسته‌بندی‌ها) توی
-- دیتابیس بودن، category_id شون NULL می‌مونه و دیگه توی لیست دسته‌بندی‌ها
-- دیده نمی‌شن (چون آن لیست با JOIN روی category_id ساخته میشه). برای
-- اینکه هیچ محصولی گم نشه، هر همچین محصولی خودکار میره توی یک دسته‌بندی
-- fallback به اسم «سایر محصولات» (که همیشه آخر لیست دسته‌بندی‌ها میاد).
-- این کار idempotent و بی‌خطره؛ سر هر بار بالا اومدن برنامه اجرا میشه.
INSERT INTO shopbot.categories (name, sort_order)
VALUES ('سایر محصولات', 999)
ON CONFLICT (name) DO NOTHING;

UPDATE shopbot.products
SET category_id = (SELECT id FROM shopbot.categories WHERE name = 'سایر محصولات')
WHERE category_id IS NULL;

CREATE TABLE IF NOT EXISTS shopbot.product_variants (
    id             SERIAL PRIMARY KEY,
    product_id     INTEGER NOT NULL REFERENCES shopbot.products(id) ON DELETE CASCADE,
    size           TEXT NOT NULL,
    color          TEXT NOT NULL,
    price_override NUMERIC(12, 0),
    stock          INTEGER NOT NULL DEFAULT 0,
    sku            TEXT UNIQUE,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (product_id, size, color)
);

-- رجیستری سبک کاربرها (فقط برای شمارش/فهرست کاربرها در پنل ادمین؛ دیگه
-- schema_name نداره چون schema جدا وجود نداره).
CREATE TABLE IF NOT EXISTS shopbot.users (
    chat_id       BIGINT PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- session فعلی مکالمه‌ی هر کاربر (یک ردیف به‌ازای هر chat_id).
CREATE TABLE IF NOT EXISTS shopbot.sessions (
    chat_id              BIGINT PRIMARY KEY,
    state                TEXT,
    intent               TEXT,
    selected_category_id INTEGER,
    selected_gender      TEXT,
    selected_product_id  INTEGER,
    selected_variant_id  INTEGER,
    pending_order_id     BIGINT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- تاریخچه‌ی کامل پیام‌ها (کاربر + دستیار) با ساعت دقیق ارسال هر پیام،
-- همه‌ی کاربرها در یک جدول با ستون chat_id ایندکس‌شده.
CREATE TABLE IF NOT EXISTS shopbot.messages (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopbot_messages_chat_created
    ON shopbot.messages (chat_id, created_at);

-- سفارش‌های همه‌ی کاربرها (همراه با خودِ عکس فیش پرداخت، نه فقط file_id
-- تلگرام که ممکنه بعد از مدتی نامعتبر بشه)، در یک جدول مشترک با chat_id
-- ایندکس‌شده. چون همه‌جا در یک جدولن، یک order_id مستقیماً و بدون نیاز به
-- نگاشت جداگانه پیدا می‌شه (id همین‌جا PRIMARY KEY است).
CREATE TABLE IF NOT EXISTS shopbot.orders (
    id                   BIGSERIAL PRIMARY KEY,
    chat_id              BIGINT NOT NULL,
    variant_id           INTEGER NOT NULL,
    product_name         TEXT NOT NULL,
    variant_desc         TEXT NOT NULL,
    unit_price           NUMERIC(12, 0) NOT NULL,
    status               TEXT NOT NULL DEFAULT 'awaiting_payment',
    payment_file_id      TEXT,
    payment_image        BYTEA,
    payment_image_mime   TEXT,
    ocr_extracted_amount NUMERIC(12, 0),
    ocr_confidence       TEXT,
    tracking_number      TEXT,
    admin_chat_id        BIGINT,
    admin_message_id     BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopbot_orders_chat_created
    ON shopbot.orders (chat_id, created_at);
-- برای گزارش فروش (فیلتر روی status بدون رفتن به ازای هر کاربر - رفع N+1).
CREATE INDEX IF NOT EXISTS idx_shopbot_orders_status
    ON shopbot.orders (status);

-- مشخصات تحویل مشتری (برای فاکتور). idempotent: روی دیتابیس‌های قدیمی هم
-- خودکار با بالا اومدن برنامه اضافه میشن.
ALTER TABLE shopbot.orders   ADD COLUMN IF NOT EXISTS customer_name    TEXT;
ALTER TABLE shopbot.orders   ADD COLUMN IF NOT EXISTS customer_address TEXT;
ALTER TABLE shopbot.sessions ADD COLUMN IF NOT EXISTS customer_name    TEXT;
ALTER TABLE shopbot.sessions ADD COLUMN IF NOT EXISTS customer_address TEXT;

-- یادآوریِ عدم‌فعالیتِ در انتظار برای هر کاربر (حداکثر یک ردیف فعال به
-- ازای هر chat_id). این جدول هم مرجع «چه کسی رو کِی یادآوری کنیم» بعد از
-- ری‌استارت پروسه‌ست و هم - چون یک background worker با claim اتمیک از
-- روی همین جدول یادآوری‌ها رو ارسال می‌کنه (به app/handlers.py نگاه کن) -
-- مکانیزم امنی برای اجرای هم‌زمان چند نمونه از بات (horizontal scaling)
-- بدون ارسال تکراری/گم‌شدن یادآوری.
CREATE TABLE IF NOT EXISTS shopbot.pending_reminders (
    chat_id  BIGINT PRIMARY KEY,
    due_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shopbot_pending_reminders_due
    ON shopbot.pending_reminders (due_at);
"""


async def _ensure_schema() -> None:
    await pool().execute(_SCHEMA_SQL)


# --------------------------------------------------------------------------
# کاربران
# --------------------------------------------------------------------------

async def ensure_user(chat_id: int) -> None:
    """مطمئن می‌شه این کاربر توی رجیستری shopbot.users ثبت شده.

    فقط یک UPSERT سبک روی یک جدول مشترک (نه ساختن schema/جدول جدید)، پس
    صدا زدنش سر هر پیام تقریباً بدون هزینه‌ست؛ برای کم کردن حتی همین
    هزینه‌ی کوچیک هم، اگه همین پروسه کاربر رو در ۵ دقیقه‌ی اخیر دیده باشه
    دوباره UPSERT نمی‌کنه (فقط یک بهینه‌سازی محلی، نه پیش‌شرط درستی)."""
    now = datetime.now()
    last = _seen_recently.get(chat_id)
    if last is not None and (now - last).total_seconds() < _SEEN_CACHE_TTL_SECONDS:
        return

    await pool().execute(
        """
        INSERT INTO shopbot.users (chat_id)
        VALUES ($1)
        ON CONFLICT (chat_id) DO UPDATE SET last_seen_at = now();
        """,
        chat_id,
    )
    _seen_recently[chat_id] = now


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

async def get_session(chat_id: int) -> dict[str, Any]:
    row = await pool().fetchrow(
        "SELECT * FROM shopbot.sessions WHERE chat_id = $1;", chat_id
    )
    if row is None:
        return {
            "chat_id": chat_id,
            "state": None,
            "intent": None,
            "selected_category_id": None,
            "selected_gender": None,
            "selected_product_id": None,
            "selected_variant_id": None,
            "pending_order_id": None,
            "customer_name": None,
            "customer_address": None,
        }
    return dict(row)


async def save_session(chat_id: int, **fields: Any) -> None:
    """Upsert only the given fields into this chat_id's session row."""
    await ensure_user(chat_id)
    current = await get_session(chat_id)
    current.update(fields)
    await pool().execute(
        """
        INSERT INTO shopbot.sessions
            (chat_id, state, intent, selected_category_id, selected_gender,
             selected_product_id, selected_variant_id, pending_order_id,
             customer_name, customer_address, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
        ON CONFLICT (chat_id) DO UPDATE SET
            state = EXCLUDED.state,
            intent = EXCLUDED.intent,
            selected_category_id = EXCLUDED.selected_category_id,
            selected_gender = EXCLUDED.selected_gender,
            selected_product_id = EXCLUDED.selected_product_id,
            selected_variant_id = EXCLUDED.selected_variant_id,
            pending_order_id = EXCLUDED.pending_order_id,
            customer_name = EXCLUDED.customer_name,
            customer_address = EXCLUDED.customer_address,
            updated_at = now();
        """,
        chat_id,
        current.get("state"),
        current.get("intent"),
        current.get("selected_category_id"),
        current.get("selected_gender"),
        current.get("selected_product_id"),
        current.get("selected_variant_id"),
        current.get("pending_order_id"),
        current.get("customer_name"),
        current.get("customer_address"),
    )


async def reset_session(chat_id: int) -> None:
    await save_session(
        chat_id,
        state=None,
        intent=None,
        selected_category_id=None,
        selected_gender=None,
        selected_product_id=None,
        selected_variant_id=None,
        pending_order_id=None,
        customer_name=None,
        customer_address=None,
    )


# --------------------------------------------------------------------------
# Conversation history
# --------------------------------------------------------------------------

async def log_message(chat_id: int, role: str, content: str) -> None:
    await ensure_user(chat_id)
    await pool().execute(
        "INSERT INTO shopbot.messages (chat_id, role, content) VALUES ($1, $2, $3);",
        chat_id, role, content,
    )


async def get_recent_history(chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """آخرین N پیام (برای دادن context به LLM)، به ترتیب زمانی (قدیم -> جدید)."""
    rows = await pool().fetch(
        """
        SELECT role, content, created_at FROM shopbot.messages
        WHERE chat_id = $1
        ORDER BY created_at DESC
        LIMIT $2;
        """,
        chat_id, limit,
    )
    return [dict(r) for r in reversed(rows)]


async def get_full_history(chat_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """کل تاریخچه‌ی پیام‌های این کاربر همراه با ساعت دقیق هر پیام
    (created_at) - برای گزارش/بررسی دستی، نه برای فید کردن به LLM."""
    rows = await pool().fetch(
        """
        SELECT id, role, content, created_at FROM shopbot.messages
        WHERE chat_id = $1
        ORDER BY created_at ASC
        LIMIT $2;
        """,
        chat_id, limit,
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Products / variants (کاتالوگ مشترک)
# --------------------------------------------------------------------------

async def list_active_products() -> list[dict[str, Any]]:
    rows = await pool().fetch(
        "SELECT * FROM shopbot.products WHERE is_active = TRUE ORDER BY id;"
    )
    return [dict(r) for r in rows]


# --- دسته‌بندی و جنسیت (مرحله‌های اول فرآیند خرید) ---------------------

async def list_categories_with_products() -> list[dict[str, Any]]:
    """فقط دسته‌بندی‌هایی که حداقل یک محصول فعال دارن، به ترتیب نمایش."""
    rows = await pool().fetch(
        """
        SELECT DISTINCT c.id, c.name, c.sort_order
        FROM shopbot.categories c
        JOIN shopbot.products p ON p.category_id = c.id AND p.is_active = TRUE
        ORDER BY c.sort_order, c.name;
        """
    )
    return [dict(r) for r in rows]


async def list_genders_for_category(category_id: int) -> list[Optional[str]]:
    """مقادیر جنسیت متمایزی که بین محصولات فعال این دسته‌بندی وجود داره.
    None به‌معنای محصولاتیه که جنسیت مشخصی ندارن (یکسان/بدون تفکیک)."""
    rows = await pool().fetch(
        """
        SELECT DISTINCT gender FROM shopbot.products
        WHERE category_id = $1 AND is_active = TRUE
        ORDER BY gender NULLS LAST;
        """,
        category_id,
    )
    return [r["gender"] for r in rows]


async def list_products_for_category(
    category_id: int, gender: Optional[str] = None, any_gender: bool = False
) -> list[dict[str, Any]]:
    """محصولات فعال یک دسته‌بندی.

    - any_gender=True: بدون فیلتر جنسیت (وقتی فقط یک مقدار جنسیت در کل
      دسته‌بندی وجود داره و نیازی به پرسیدن نیست).
    - any_gender=False و gender=None: فقط محصولاتی که جنسیت ندارن (یکسان).
    - any_gender=False و gender مشخص: فقط همون جنسیت.
    """
    if any_gender:
        rows = await pool().fetch(
            "SELECT * FROM shopbot.products WHERE category_id = $1 AND is_active = TRUE ORDER BY id;",
            category_id,
        )
    elif gender is None:
        rows = await pool().fetch(
            """
            SELECT * FROM shopbot.products
            WHERE category_id = $1 AND is_active = TRUE AND gender IS NULL
            ORDER BY id;
            """,
            category_id,
        )
    else:
        rows = await pool().fetch(
            """
            SELECT * FROM shopbot.products
            WHERE category_id = $1 AND is_active = TRUE AND gender = $2
            ORDER BY id;
            """,
            category_id, gender,
        )
    return [dict(r) for r in rows]


async def get_category(category_id: int) -> Optional[dict[str, Any]]:
    row = await pool().fetchrow("SELECT * FROM shopbot.categories WHERE id = $1;", category_id)
    return dict(row) if row else None


async def list_variants_for_product(product_id: int) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT * FROM shopbot.product_variants
        WHERE product_id = $1 AND is_active = TRUE
        ORDER BY id;
        """,
        product_id,
    )
    return [dict(r) for r in rows]


async def get_variant(variant_id: int) -> Optional[dict[str, Any]]:
    row = await pool().fetchrow(
        "SELECT * FROM shopbot.product_variants WHERE id = $1;", variant_id
    )
    return dict(row) if row else None


async def get_product(product_id: int) -> Optional[dict[str, Any]]:
    row = await pool().fetchrow("SELECT * FROM shopbot.products WHERE id = $1;", product_id)
    return dict(row) if row else None


async def effective_price(variant: dict[str, Any], product: dict[str, Any]) -> int:
    return int(variant["price_override"] or product["base_price"])


async def decrement_stock(variant_id: int) -> bool:
    """Atomically reduce stock by 1. Returns False if it was already 0."""
    result = await pool().execute(
        """
        UPDATE shopbot.product_variants
        SET stock = stock - 1
        WHERE id = $1 AND stock > 0;
        """,
        variant_id,
    )
    return result.endswith("1")


# --- کاتالوگ برای LLM: یک کوئری واحد (بدون N+1) + کش کوتاه‌مدت -----------
#
# قبلاً catalog_summary_text به‌ازای هر محصول یک کوئری جدا برای گرفتن
# variantها می‌زد (N+1) و هیچ کشی هم نداشت، یعنی برای *هر* پیام معمولی
# کاربر (که به chat_node می‌رسه) این کوئری‌ها از نو اجرا می‌شدن. الان:
#   ۱. تمام محصولات + variantهای فعالشون در یک کوئری (LEFT JOIN) خونده میشه.
#   ۲. نتیجه‌ی متنی نهایی برای مدت کوتاهی (CATALOG_CACHE_TTL_SECONDS) در
#      حافظه‌ی پروسه کش میشه، چون کاتالوگ خیلی کمتر از پیام‌های کاربر تغییر
#      می‌کنه؛ با انقضای TTL، دفعه‌ی بعد خودکار دوباره تازه ساخته میشه.
_catalog_cache_text: Optional[str] = None
_catalog_cache_at: Optional[datetime] = None


async def _catalog_rows() -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await pool().fetch(
            """
            SELECT
                p.id AS product_id,
                p.name,
                p.base_price,
                p.category_id,
                p.gender,
                v.size,
                v.stock
            FROM shopbot.products p
            LEFT JOIN shopbot.product_variants v
                ON v.product_id = p.id AND v.is_active = TRUE
            WHERE p.is_active = TRUE
            ORDER BY p.id;
            """
        )
    ]


async def catalog_summary_text(force_refresh: bool = False) -> str:
    """Human-readable catalog snapshot to feed the LLM as context.

    یک کوئری واحد (بدون N+1) + کش کوتاه‌مدت در حافظه‌ی پروسه."""
    global _catalog_cache_text, _catalog_cache_at

    now = datetime.now()
    if (
        not force_refresh
        and _catalog_cache_text is not None
        and _catalog_cache_at is not None
        and (now - _catalog_cache_at).total_seconds() < config.CATALOG_CACHE_TTL_SECONDS
    ):
        return _catalog_cache_text

    rows = await _catalog_rows()
    if not rows:
        text = "در حال حاضر هیچ محصولی در فروشگاه ثبت نشده."
        _catalog_cache_text, _catalog_cache_at = text, now
        return text

    categories = {c["id"]: c["name"] for c in await list_categories_with_products()}

    products: dict[int, dict[str, Any]] = {}
    for r in rows:
        p = products.setdefault(
            r["product_id"],
            {
                "name": r["name"],
                "base_price": r["base_price"],
                "category_id": r["category_id"],
                "gender": r["gender"],
                "sizes_in_stock": set(),
            },
        )
        if r["size"] is not None and (r["stock"] or 0) > 0:
            p["sizes_in_stock"].add(r["size"])

    lines = []
    for p in products.values():
        sizes = ", ".join(sorted(p["sizes_in_stock"])) or "ناموجود"
        cat_name = categories.get(p["category_id"], "متفرقه")
        gender_part = f" ({p['gender']})" if p.get("gender") else ""
        lines.append(
            f"- [{cat_name}{gender_part}] {p['name']}: از {p['base_price']:,} تومان | سایزهای موجود: {sizes}"
        )
    text = "\n".join(lines)
    _catalog_cache_text, _catalog_cache_at = text, now
    return text


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

async def create_order(
    chat_id: int,
    variant: dict,
    product: dict,
    customer_name: Optional[str] = None,
    customer_address: Optional[str] = None,
) -> int:
    await ensure_user(chat_id)
    price = int(variant["price_override"] or product["base_price"])
    variant_desc = f"سایز {variant['size']} / {variant['color']}"

    order_id = await pool().fetchval(
        """
        INSERT INTO shopbot.orders
            (chat_id, variant_id, product_name, variant_desc, unit_price,
             customer_name, customer_address)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id;
        """,
        chat_id, variant["id"], product["name"], variant_desc, price,
        customer_name, customer_address,
    )
    return order_id


async def get_order(order_id: int) -> Optional[dict[str, Any]]:
    row = await pool().fetchrow("SELECT * FROM shopbot.orders WHERE id = $1;", order_id)
    return dict(row) if row else None


async def update_order(order_id: int, **fields: Any) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    await pool().execute(
        f"UPDATE shopbot.orders SET {set_clause}, updated_at = now() WHERE id = $1;",
        order_id, *fields.values(),
    )


async def list_orders_for_chat(chat_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT * FROM shopbot.orders
        WHERE chat_id = $1
        ORDER BY created_at DESC
        LIMIT $2;
        """,
        chat_id, limit,
    )
    return [dict(r) for r in rows]


async def cancel_order(order_id: int) -> None:
    await update_order(order_id, status="cancelled")


# --------------------------------------------------------------------------
# یادآوری‌های در انتظار
# --------------------------------------------------------------------------

async def set_pending_reminder(chat_id: int, due_at: datetime) -> None:
    """موعد بعدیِ یادآوریِ این کاربر رو ثبت/جایگزین می‌کنه (این خودش، بدون
    نیاز به هیچ زمان‌بند در-حافظه‌ای مثل JobQueue، تایمر رو «ریست» می‌کنه)."""
    await pool().execute(
        """
        INSERT INTO shopbot.pending_reminders (chat_id, due_at)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET due_at = EXCLUDED.due_at;
        """,
        chat_id, due_at,
    )


async def clear_pending_reminder(chat_id: int) -> None:
    await pool().execute(
        "DELETE FROM shopbot.pending_reminders WHERE chat_id = $1;", chat_id
    )


async def claim_due_reminders(limit: int = 50) -> list[dict[str, Any]]:
    """موعدرسیده‌ها رو به‌صورت اتمیک «برمی‌داره» (SELECT ... FOR UPDATE
    SKIP LOCKED و بعد DELETE در یک statement) و برمی‌گردونه.

    این الگو دقیقاً همون چیزیه که اجرای هم‌زمان چند نمونه از بات
    (horizontal scaling، مثلاً چند replica پشت یک PaaS) رو امن می‌کنه: اگه
    دو نمونه دقیقاً هم‌زمان این تابع رو صدا بزنن، PostgreSQL تضمین می‌کنه
    هر ردیفِ در انتظار فقط توسط یکی از اون دو claim (و حذف) بشه، پس هیچ
    کاربری یادآوری تکراری نمی‌گیره؛ چون خودِ ردیف منبع حقیقتِ زمان‌بندیه
    (نه حافظه‌ی یک پروسه‌ی خاص)، ری‌استارت یا افزایش/کاهش تعداد نمونه‌ها هم
    هیچ یادآوری‌ای رو گم نمی‌کنه."""
    rows = await pool().fetch(
        """
        DELETE FROM shopbot.pending_reminders
        WHERE chat_id IN (
            SELECT chat_id FROM shopbot.pending_reminders
            WHERE due_at <= now()
            ORDER BY due_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING chat_id, due_at;
        """,
        limit,
    )
    return [dict(r) for r in rows]
