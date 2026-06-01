#!/bin/bash
# Thin wrapper so cron doesn't need to set a working directory.
# Called every 15 minutes to fetch the next batch of stalest ASINs.
cd /Users/cagri/Desktop/amazon-tracker/pipeline
exec /usr/bin/python3 keepa_api_offers.py --tick
