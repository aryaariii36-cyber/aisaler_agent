"""
دسترسی بات پنل ادمین به همون PostgreSQL بات اصلی.

معماری دیتابیس (یادآوری از بات اصلی - app/db.py آنجا مرجع کامله):
- همه‌ی کاربرها/سفارش‌ها/پیام‌ها در جدول‌های مشترک schema ``shopbot`` با
  ستون ``chat_id`` ایندکس‌شده نگه داشته می‌شن (نه یک schema جدا به‌ازای هر
  کاربر). یعنی ``shopbot.users`` / ``shopbot.sessions`` / ``shopbot.messages``
  / ``shopbot.orders`` / ``shopbot.user_profile``.
- ``shopbot.products`` / ``shopbot.product_variants`` کاتالوگ مشترکه.

این ماژول اساساً فقط می‌خونه؛ تنها استثنا کش تحلیل «خصوصیات کاربر»
(جدول shopbot.user_profile) است که توسط app/profile_agent.py نوشته می‌شه
تا لازم نباشه هر بار از نو با مدل زبانی ساخته بشه.
"""

from typing import Any, Optional

import asyncpg

from . import config

_pool: Optional[asyncpg.Pool] = None


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
    # این بات فقط شاید یکی/دو ستونی که خودش می‌نویسه (user_profile) رو
    # نیاز داره؛ اگه بات اصلی هنوز یک‌بار هم اجرا نشده باشه (deploy اول)،
    # این IF NOT EXISTS idempotent مطمئن می‌شه جدول موردنیازش موجوده.
    await pool().execute(_USER_PROFILE_TABLE_SQL)


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool آماده نیست - init_pool() صدا زده نشده.")
    return _pool


# --------------------------------------------------------------------------
# کاربران
# --------------------------------------------------------------------------

async def count_users() -> int:
    return await pool().fetchval("SELECT count(*) FROM shopbot.users;")


async def list_users(offset: int = 0, limit: int = 8) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT chat_id, first_seen_at, last_seen_at
        FROM shopbot.users
        ORDER BY first_seen_at DESC
        OFFSET $1 LIMIT $2;
        """,
        offset, limit,
    )
    return [dict(r) for r in rows]


async def get_user_full_info(chat_id: int) -> Optional[dict[str, Any]]:
    """اطلاعات کامل یک کاربر (به‌جز تاریخچه چت): مشخصات ثبت‌نام + سفارش‌ها."""
    reg = await pool().fetchrow(
        "SELECT chat_id, first_seen_at, last_seen_at FROM shopbot.users WHERE chat_id = $1;",
        chat_id,
    )
    if reg is None:
        return None
    orders = await pool().fetch(
        """
        SELECT id, product_name, variant_desc, unit_price, status,
               tracking_number, created_at, updated_at
        FROM shopbot.orders
        WHERE chat_id = $1
        ORDER BY created_at DESC;
        """,
        chat_id,
    )
    session = await pool().fetchrow(
        "SELECT state, intent FROM shopbot.sessions WHERE chat_id = $1;", chat_id
    )
    return {
        **dict(reg),
        "orders": [dict(o) for o in orders],
        "session": dict(session) if session else None,
    }


async def get_latest_order_with_payment(chat_id: int) -> Optional[dict[str, Any]]:
    row = await pool().fetchrow(
        """
        SELECT id, product_name, variant_desc, unit_price, status,
               payment_image, payment_image_mime, ocr_extracted_amount,
               ocr_confidence, created_at
        FROM shopbot.orders
        WHERE chat_id = $1 AND payment_image IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1;
        """,
        chat_id,
    )
    return dict(row) if row else None


async def get_full_history(chat_id: int, limit: int = 1000) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT role, content, created_at
        FROM shopbot.messages
        WHERE chat_id = $1
        ORDER BY created_at ASC
        LIMIT $2;
        """,
        chat_id, limit,
    )
    return [dict(r) for r in rows]


async def get_message_count(chat_id: int) -> int:
    return await pool().fetchval(
        "SELECT count(*) FROM shopbot.messages WHERE chat_id = $1;", chat_id
    )


async def get_order_count(chat_id: int) -> int:
    return await pool().fetchval(
        "SELECT count(*) FROM shopbot.orders WHERE chat_id = $1;", chat_id
    )


async def get_all_orders(chat_id: int) -> list[dict[str, Any]]:
    """همه‌ی سفارش‌های کاربر (بدون محدودیت تعداد) - برای تحلیل خصوصیات کاربر."""
    rows = await pool().fetch(
        """
        SELECT id, product_name, variant_desc, unit_price, status, created_at
        FROM shopbot.orders
        WHERE chat_id = $1
        ORDER BY created_at ASC;
        """,
        chat_id,
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# کش «خصوصیات کاربر» (تنها جدولی که این بات پنل ادمین توش می‌نویسه)
# --------------------------------------------------------------------------

_USER_PROFILE_TABLE_SQL = """
CREATE SCHEMA IF NOT EXISTS shopbot;

CREATE TABLE IF NOT EXISTS shopbot.user_profile (
    chat_id               BIGINT PRIMARY KEY,
    profile_text          TEXT NOT NULL,
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_message_count  INTEGER NOT NULL DEFAULT 0,
    source_order_count    INTEGER NOT NULL DEFAULT 0
);
"""


async def get_cached_profile(chat_id: int) -> Optional[dict[str, Any]]:
    row = await pool().fetchrow(
        "SELECT * FROM shopbot.user_profile WHERE chat_id = $1;", chat_id
    )
    return dict(row) if row else None


async def save_profile(chat_id: int, profile_text: str, message_count: int, order_count: int) -> None:
    await pool().execute(
        """
        INSERT INTO shopbot.user_profile
            (chat_id, profile_text, generated_at, source_message_count, source_order_count)
        VALUES ($1, $2, now(), $3, $4)
        ON CONFLICT (chat_id) DO UPDATE SET
            profile_text = EXCLUDED.profile_text,
            generated_at = now(),
            source_message_count = EXCLUDED.source_message_count,
            source_order_count = EXCLUDED.source_order_count;
        """,
        chat_id, profile_text, message_count, order_count,
    )


# --------------------------------------------------------------------------
# گزارش فروش
# --------------------------------------------------------------------------
#
# قبلاً این تابع اول لیست تمام schemaهای کاربرها رو می‌گرفت و بعد به‌ازای
# *هر* کاربر یک round-trip جدا به دیتابیس می‌زد (N+1) - یعنی زمان تولید
# گزارش با تعداد کل کاربرهای ثبت‌نامی رشد می‌کرد، نه با تعداد سفارش‌های
# تاییدشده. با معماری جدول مشترک، همون کار با یک کوئری واحد (با ایندکس
# روی status) در O(1) round-trip انجام میشه.

async def sales_report(limit: int = 300) -> dict[str, Any]:
    summary = await pool().fetchrow(
        """
        SELECT count(*) AS cnt, coalesce(sum(unit_price), 0) AS total
        FROM shopbot.orders
        WHERE status = 'paid_verified';
        """
    )
    rows = await pool().fetch(
        """
        SELECT id, chat_id, product_name, variant_desc, unit_price, created_at
        FROM shopbot.orders
        WHERE status = 'paid_verified'
        ORDER BY created_at DESC
        LIMIT $1;
        """,
        limit,
    )
    return {
        "count": summary["cnt"],
        "total_revenue": int(summary["total"]),
        "items": [dict(r) for r in rows],
    }


# --------------------------------------------------------------------------
# موجودی انبار (کاتالوگ مشترک)
# --------------------------------------------------------------------------

async def inventory_snapshot() -> list[dict[str, Any]]:
    # این تابع جزو ۵ مشکل گزارش‌شده نیست؛ عمداً همون رفتار قبلی (کوئری
    # جدا برای variantهای هر محصول) نگه داشته شده تا فقط مشکلات مشخص‌شده
    # تغییر کنن.
    products = await pool().fetch(
        "SELECT id, name, base_price, is_active FROM shopbot.products ORDER BY id;"
    )
    result = []
    for p in products:
        variants = await pool().fetch(
            """
            SELECT size, color, stock, price_override, sku, is_active
            FROM shopbot.product_variants
            WHERE product_id = $1
            ORDER BY id;
            """,
            p["id"],
        )
        result.append({**dict(p), "variants": [dict(v) for v in variants]})
    return result
