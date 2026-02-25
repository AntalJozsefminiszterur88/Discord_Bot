from bot_app.core import TOKEN, bot

# Import modules for side effects (command/event registration).
import bot_app.automation  # noqa: F401
import bot_app.commands  # noqa: F401


def run() -> None:
    bot.run(TOKEN)
