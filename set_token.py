import getpass
import logging
import os
import re

from config import setup_logging

setup_logging()
log = logging.getLogger(__name__)

path = os.path.expanduser("~/.zshrc")
content = open(path).read()

log.info("Paste your Telegram token and press Enter (input is hidden):")
token = getpass.getpass("")

if not token:
    log.warning("No token entered, aborting.")
    raise SystemExit(1)

new_content = re.sub(r'export TELEGRAM_TOKEN=.*', f'export TELEGRAM_TOKEN="{token.strip()}"', content)
open(path, "w").write(new_content)
log.info("Saved to ~/.zshrc")
