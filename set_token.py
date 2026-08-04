import getpass
import os
import re

path = os.path.expanduser("~/.zshrc")
content = open(path).read()

print("Paste your Telegram token and press Enter (input is hidden):")
token = getpass.getpass("")

if not token:
    print("No token entered, aborting.")
    exit(1)

new_content = re.sub(r'export TELEGRAM_TOKEN=.*', f'export TELEGRAM_TOKEN="{token.strip()}"', content)
open(path, "w").write(new_content)
print("Saved to ~/.zshrc")
