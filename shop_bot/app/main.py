import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import config, db, handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    await db.init_pool()
    log.info("DB pool initialised.")
    # background worker مستقل از JobQueue که هر REMINDER_POLL_INTERVAL_SECONDS
    # ثانیه یک‌بار یادآوری‌های موعدرسیده رو از دیتابیس claim و ارسال می‌کنه.
    # با app.create_task ثبت می‌شه تا خودکار با shutdown برنامه cancel بشه؛
    # چون منبع حقیقتش جدول shopbot.pending_reminders است (نه حافظه‌ی پروسه)،
    # نیازی به یک مرحله‌ی جدای «reschedule بعد از ری‌استارت» هم نیست و روی
    # چند نمونه‌ی هم‌زمان از بات (مقیاس افقی) هم امن کار می‌کنه.
    app.create_task(
        handlers.reminder_dispatch_loop(app.bot, config.REMINDER_POLL_INTERVAL_SECONDS),
        name="reminder_dispatch_loop",
    )


async def _post_shutdown(app: Application) -> None:
    await db.close_pool()


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("cancel", handlers.cmd_cancel))

    # Admin approve/reject callbacks must be matched before the generic one.
    app.add_handler(CallbackQueryHandler(handlers.on_admin_callback, pattern=r"^admin_(approve|reject)_\d+$"))
    app.add_handler(CallbackQueryHandler(handlers.on_callback))

    app.add_handler(MessageHandler(filters.PHOTO, handlers.on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_message))

    return app


def main() -> None:
    app = build_application()
    webhook_url = config.WEBHOOK_BASE_URL.rstrip("/") + config.WEBHOOK_PATH

    log.info("Starting webhook at %s", webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=config.PORT,
        url_path=config.WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        secret_token=config.WEBHOOK_SECRET,
    )


if __name__ == "__main__":
    main()
