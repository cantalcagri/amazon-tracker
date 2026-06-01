"""
Keepa Product Viewer — CSV export automation.

Attaches to your already-running Chrome (CDP on port 9222), opens the Keepa
viewer pre-loaded with the ASINs from asins.txt, clicks Export -> CSV ->
Export, and saves the resulting CSV to pipeline/keepa_exports/.

Prereqs (same as browser_collector.py):
    1. Launch Chrome:  open -a "Google Chrome" --args --remote-debugging-port=9222
    2. Make sure you're logged into keepa.com in that Chrome
    3. Run:  python keepa_viewer_export.py [--asins-file asins.txt]
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("keepa_viewer")

HERE = Path(__file__).parent
DEFAULT_ASINS = HERE / "asins.txt"
DOWNLOAD_DIR = HERE / "keepa_exports"
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def read_asins(path: Path) -> list[str]:
    asins: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ASIN_RE.match(line) and line not in seen:
            asins.append(line)
            seen.add(line)
    return asins


def build_viewer_url(asins: list[str]) -> str:
    payload = {"1": asins, "includeInaccessibleAsins": False}
    # Keepa expects the JSON percent-encoded in the URL fragment, with
    # quote marks encoded as %22 (matches what the site itself produces).
    encoded = urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe="")
    return f"https://keepa.com/#!viewer/{encoded}"


def connect_cdp(port: int) -> webdriver.Chrome:
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.set_page_load_timeout(120)
    log.info("Attached to Chrome on port %d", port)
    return driver


def set_download_dir(driver: webdriver.Chrome, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    driver.execute_cdp_cmd(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(path.resolve()), "eventsEnabled": True},
    )
    log.info("Download dir set to %s", path)


def open_viewer_tab(driver: webdriver.Chrome, url: str) -> None:
    driver.switch_to.new_window("tab")
    driver.get(url)
    log.info("Opened viewer (URL length=%d)", len(url))


def wait_for_table(driver: webdriver.Chrome, timeout: int = 180) -> None:
    """Wait until Keepa has finished loading the product list."""
    log.info("Waiting for product table to load (up to %ds)...", timeout)
    # The viewer renders rows with class containing 'ag-row' (ag-grid) once data arrives.
    # We also accept any export toolbar item becoming clickable as success.
    end = time.time() + timeout
    while time.time() < end:
        try:
            rows = driver.execute_script(
                "return document.querySelectorAll('.ag-row, .product-row, tr[role=row]').length;"
            )
            if rows and rows > 0:
                log.info("Table rendered with %d rows", rows)
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("Product table did not load within timeout")


def click_by_text(driver: webdriver.Chrome, text: str, tag: str = "*", timeout: int = 30):
    """Click the first visible element whose trimmed text equals `text`."""
    xpath = (
        f"//{tag}[normalize-space(.)='{text}' and not(ancestor::*[contains(@style,'display: none')])]"
    )
    el = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xpath))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    el.click()
    return el


def trigger_export(driver: webdriver.Chrome) -> None:
    # Open the export popup. Selenium's native click on `.tool__export`
    # gets intercepted by a sibling overlay div, so we click the inner
    # `.trigger` span via JS (bypasses pointer-event hit testing).
    log.info("Opening export popup (.tool__export .trigger)...")
    trigger = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".tool__export .trigger"))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", trigger)

    # CSV is the default format (second `name=format` radio is pre-checked),
    # so we don't touch the radios. Wait for the submit button (stable id)
    # and click it via JS as well.
    log.info("Waiting for #exportSubmit ...")
    submit = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.ID, "exportSubmit"))
    )
    driver.execute_script("arguments[0].click();", submit)
    log.info("Export submitted; waiting for download to land")


def wait_for_download(path: Path, before: set[str], timeout: int = 300) -> Path:
    log.info("Waiting for CSV to appear in %s ...", path)
    end = time.time() + timeout
    while time.time() < end:
        current = {p.name for p in path.iterdir()}
        new = current - before
        finished = [p for p in (path / n for n in new) if p.suffix == ".csv" and not p.name.endswith(".crdownload")]
        if finished:
            # Take newest if multiple.
            return max(finished, key=lambda p: p.stat().st_mtime)
        time.sleep(1)
    raise TimeoutError("CSV download did not complete in time")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asins-file", type=Path, default=DEFAULT_ASINS)
    ap.add_argument("--connect-port", type=int, default=9222)
    ap.add_argument("--download-dir", type=Path, default=DOWNLOAD_DIR)
    ap.add_argument("--keep-tab", action="store_true", help="Don't close the viewer tab when done")
    ap.add_argument("--no-import", action="store_true",
                    help="Skip importing the CSV into SQLite (just download).")
    args = ap.parse_args()

    asins = read_asins(args.asins_file)
    if not asins:
        log.error("No ASINs found in %s", args.asins_file)
        return 1
    log.info("Loaded %d ASINs from %s", len(asins), args.asins_file)

    args.download_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in args.download_dir.iterdir()}

    driver = connect_cdp(args.connect_port)
    try:
        set_download_dir(driver, args.download_dir)
        url = build_viewer_url(asins)
        open_viewer_tab(driver, url)

        # Keepa's hash router sometimes needs a nudge to fire.
        time.sleep(2)
        wait_for_table(driver)

        trigger_export(driver)
        csv_path = wait_for_download(args.download_dir, before)

        # Rename with timestamp + asin-count for traceability.
        stamped = csv_path.with_name(
            f"keepa_viewer_{datetime.now():%Y%m%d_%H%M%S}_{len(asins)}asins.csv"
        )
        csv_path.rename(stamped)
        log.info("Downloaded: %s (%.1f KB)", stamped, stamped.stat().st_size / 1024)
        print(str(stamped))

        if not args.no_import:
            from keepa_csv_importer import import_csv
            n = import_csv(stamped)
            log.info("Imported %d rows into fct_keepa_daily", n)

        if not args.keep_tab:
            try:
                driver.close()
            except Exception:
                pass
        return 0
    finally:
        # Detach (don't quit — that would kill your Chrome).
        try:
            driver.service.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
