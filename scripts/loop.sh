#!/bin/bash
# Continuous token-aware runner for the per-seller API collector.
# Supervised by launchd (scripts/com.amazontracker.loop.plist) with KeepAlive,
# so it restarts on crash and at login/reboot.
#
# The loop itself decides cadence: it batches whenever the Keepa balance is
# >= KEEPA_MIN_TOKENS (default 150) and sleeps precisely for refill otherwise.
cd /Users/cagri/Desktop/amazon-tracker/pipeline
exec /usr/bin/python3 keepa_api_offers.py --loop
