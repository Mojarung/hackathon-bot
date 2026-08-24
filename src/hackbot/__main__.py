"""Entry point: Telegram polling, the scheduler and the web server share one loop.

Long polling rather than webhooks because the deployment has no domain and no
certificate. uvicorn must stay single-worker: two processes calling getUpdates
with the same token is a fight Telegram settles by rejecting one of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn
from fastapi import FastAPI

from hackbot.bot.factory import build_bot, build_dispatcher, publish_commands
from hackbot.config import get_settings
from hackbot.db.base import dispose_db, init_db, session_scope
from hackbot.domain.services.events import resync_future_reminders
from hackbot.logging_setup import setup_logging
from hackbot.scheduler.runner import scheduler_loop
from hackbot.web.app import create_app

log = logging.getLogger(__name__)


def build_application() -> FastAPI:
    settings = get_settings()
    app = create_app()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        await init_db()
        settings.abs_files_dir.mkdir(parents=True, exist_ok=True)

        # Events created under an older reminder ladder get the current one.
        async with session_scope() as session:
            touched = await resync_future_reminders(session)
        if touched:
            log.info("resynced reminders for %s upcoming events", touched)

        bot = build_bot()
        dispatcher = build_dispatcher()
        stop = asyncio.Event()

        me = await bot.me()
        log.info("starting as @%s (id=%s)", me.username, me.id)
        if not me.can_read_all_group_messages:
            log.warning(
                "privacy mode is ON - the bot will only see commands, mentions and replies"
            )
        await publish_commands(bot)

        # handle_signals=False: uvicorn owns SIGINT/SIGTERM, and two handlers
        # fighting over the same loop makes shutdown hang.
        polling = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False),
            name="polling",
        )
        ticker = asyncio.create_task(scheduler_loop(bot, stop), name="scheduler")

        try:
            yield
        finally:
            log.info("shutting down")
            stop.set()
            with contextlib.suppress(Exception):
                await dispatcher.stop_polling()
            for task in (polling, ticker):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await bot.session.close()
            await dispose_db()

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        build_application(),
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
