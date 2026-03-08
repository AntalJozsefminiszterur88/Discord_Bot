from bot_app.core import TOKEN, bot, logger

# Import modules for side effects (command/event registration).
import bot_app.automation  # noqa: F401
import bot_app.commands  # noqa: F401


def run() -> None:
    logger.info("Starting Discord bot process.")
    bot.run(TOKEN)
