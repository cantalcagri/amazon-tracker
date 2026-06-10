#!/bin/bash
# Second API key loop — slot 2 of 2.
# Runs alongside loop.sh (slot 1). Each slot owns half the ASIN catalog
# (by ASIN hash) so they NEVER request the same ASIN or waste each other's tokens.
#
# To enable:
#   1. Add KEEPA_API_KEY_2=<your_second_key> to pipeline/.env
#   2. Load the launchd supervisor:
#        cp scripts/com.amazontracker.loop2.plist ~/Library/LaunchAgents/
#        launchctl load ~/Library/LaunchAgents/com.amazontracker.loop2.plist
#   3. Or just run manually: bash scripts/loop2.sh
cd /Users/cagri/Desktop/amazon-tracker/pipeline
exec /usr/bin/python3 keepa_api_offers.py --loop --slot 2 --n-slots 2
