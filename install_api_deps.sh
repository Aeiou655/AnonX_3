#!/bin/bash
set -euo pipefail

# The API stack is part of the tested project lock.
python3 -m pip install --requirement requirements.txt
python3 -m pip check

echo "Done. Restart the bot: python3 -m AnonX_3"
