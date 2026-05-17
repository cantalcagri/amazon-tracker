"""
Amazon Seller Tracker — Browser Collector (Selenium + Keepa Extension)
=======================================================================
Launches YOUR existing Chrome browser with your Keepa extension already
installed. No API key needed. Keepa injects BSR graphs + stock estimates
directly into the Amazon page — we read from the DOM.

Requirements:
    pip install selenium webdriver-manager python-dotenv schedule

First-time setup:
    1. Find your Chrome profile path (see SETUP section in README)
    2. Add it to pipeline/.env as CHROME_PROFILE_PATH=...
    3. Run: python browser_collector.py --asin B0CV3CDPTK

IMPORTANT: Close Chrome completely before running the script.
           Chrome can't share a profile between two instances.
"""

import os
import re
import time
import json
import random
import logging
import sqlite3
import schedule
import argparse
import platform
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("tracker.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ──────────────────────────────
# CONFIG
# ──────────────────────────────
DB_PATH            = os.getenv("DB_PATH", "amazon_tracker.db")
CHROME_PROFILE     = os.getenv("CHROME_PROFILE_PATH", "")   # set in .env
KEEPA_USERNAME     = os.getenv("KEEPA_USERNAME", "")
KEEPA_PASSWORD     = os.getenv("KEEPA_PASSWORD", "")
RETENTION_DAYS     = 10
PAGE_LOAD_TIMEOUT  = 90
WAIT_TIMEOUT       = 45

# Delays to appear human (seconds)
DELAY_BETWEEN_PAGES  = (2, 5)
DELAY_BETWEEN_ASINS  = (5, 10)
DELAY_AFTER_SCROLL   = (0.5, 1.5)


def human_delay(range_s: tuple = (2, 5)):
    time.sleep(random.uniform(*range_s))


# ──────────────────────────────
# CHROME PROFILE AUTO-DETECT
# ──────────────────────────────
def detect_chrome_profile() -> str:
    """Auto-detect the default Chrome profile path for this OS."""
    system = platform.system()
    home = Path.home()
    candidates = {
        "Darwin":  home / "Library/Application Support/Google/Chrome",
        "Windows": Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
        "Linux":   home / ".config/google-chrome",
    }
    path = candidates.get(system)
    if path and path.exists():
        return str(path)
    log.warning(
        "Could not auto-detect Chrome profile. "
        "Set CHROME_PROFILE_PATH in your .env file manually."
    )
    return ""


# ──────────────────────────────
# BROWSER SETUP
# ──────────────────────────────
KEEPA_EXTENSION_ID = "neebplgakaahbhdphmkckjjcegoiijjo"

# Persistent tracker profile — Chrome saves session cookies here so Keepa
# stays logged in between runs. Never touches the user's real Chrome profile.
TRACKER_PROFILE_DIR = Path(__file__).parent / ".chrome_profile"


def _find_keepa_path(chrome_user_data_dir: str) -> Optional[str]:
    """Search all Chrome profiles (Default, Profile 1, Profile 9 …) for Keepa extension files."""
    base = Path(chrome_user_data_dir)
    profile_dirs = [base / "Default"] + sorted(base.glob("Profile *"))
    for profile_dir in profile_dirs:
        ext_root = profile_dir / "Extensions" / KEEPA_EXTENSION_ID
        if ext_root.exists():
            versions = sorted(ext_root.iterdir(), reverse=True)
            if versions:
                log.info("Keepa found in profile: %s", profile_dir.name)
                return str(versions[0])
    return None


def _clear_tracker_profile_locks():
    """Remove stale LevelDB LOCK / Singleton files from the tracker's own Chrome profile."""
    removed = 0
    for pattern in ["**/LOCK", "SingletonLock", "SingletonSocket", "SingletonCookie"]:
        for f in TRACKER_PROFILE_DIR.glob(pattern):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        log.info("Cleared %d Chrome lock file(s) from tracker profile", removed)


def create_driver_cdp(port: int = 9222) -> webdriver.Chrome:
    """
    Connect to an ALREADY-RUNNING Chrome instance via CDP.
    User must start Chrome with --remote-debugging-port=9222.
    This preserves all existing logins (Amazon, Keepa, etc.).
    """
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    log.info("Connected to existing Chrome on port %d", port)

    # Switch to a navigatable tab (skip chrome:// and extension pages)
    handles = driver.window_handles
    switched = False
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            url = driver.execute_script("return window.location.href;")
            if url and url.startswith("http"):
                switched = True
                break
        except Exception:
            continue
    if not switched:
        # Open a fresh tab if no http tab found
        driver.execute_script("window.open('about:blank','_blank');")
        driver.switch_to.window(driver.window_handles[-1])

    return driver


def create_driver(headless: bool = False) -> webdriver.Chrome:
    """
    Launch Chrome using the tracker's own persistent profile (.chrome_profile/).
    Keepa extension files are loaded read-only from the user's real Chrome profile.
    The tracker profile saves Keepa's login session between runs.
    """
    TRACKER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _clear_tracker_profile_locks()

    log.info("Using tracker Chrome profile: %s", TRACKER_PROFILE_DIR)

    chrome_dir = CHROME_PROFILE or detect_chrome_profile()
    keepa_path = _find_keepa_path(chrome_dir) if chrome_dir else None
    if keepa_path:
        log.info("Keepa extension loaded (read-only) from: %s", keepa_path)
    else:
        log.warning("Keepa extension not found — stock data will be unavailable")

    log.info("====== WebDriver manager ======")
    opts = Options()
    opts.add_argument(f"--user-data-dir={TRACKER_PROFILE_DIR}")
    if keepa_path:
        opts.add_argument(f"--load-extension={keepa_path}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1400,900")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def extract_variation_from_title(title: str) -> dict:
    """
    Fallback for standalone listings (parent_asin == asin) where the variation
    widget is absent. Extracts size and color from the product title.
    """
    if not title:
        return {}
    result = {}
    # Size — ordered longest-first so "X-Large" matches before "Large"
    SIZE_PATTERNS = [
        r'\b(3X-?Large|3XL|XXXL)\b',
        r'\b(2X-?Large|2XL|XXL)\b',
        r'\b(X-?Large|XL)\b',
        r'\b(X-?Small|XS)\b',
        r'\b(Medium|MED)\b',
        r'\b(Small|SM)\b',
        r'\b(Large|LG)\b',
        r'\b(One\s*Size|OS)\b',
    ]
    for pat in SIZE_PATTERNS:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            result["variation_size"] = m.group(0).strip()
            break
    # Color — common apparel colors
    COLORS = [
        "Black", "White", "Gray", "Grey", "Navy", "Blue", "Red", "Green",
        "Yellow", "Orange", "Purple", "Pink", "Brown", "Beige", "Tan",
        "Olive", "Teal", "Burgundy", "Maroon", "Charcoal", "Ivory", "Cream",
        "Khaki", "Camo", "Camouflage", "Multicolor",
    ]
    m = re.search(r'\b(' + '|'.join(COLORS) + r')\b', title, re.IGNORECASE)
    if m:
        result["variation_color"] = m.group(0).strip().title()
    return result


# ──────────────────────────────
# AMAZON PAGE PARSERS
# ──────────────────────────────
class AmazonPageParser:
    """Parses Amazon product + offers pages after Keepa has injected its data."""

    def __init__(self, driver: webdriver.Chrome):
        self.driver = driver
        self.wait   = WebDriverWait(driver, WAIT_TIMEOUT)

    # ── Navigate ──────────────────────────────────
    def _wait_for_keepa_injection(self, wait_seconds: int = 15):
        """
        Flat wait after panel opens — gives Amazon time to render seller rows
        and Keepa time to inject Stock numbers. 15s is the proven minimum.
        """
        time.sleep(wait_seconds)
        blocks = self.driver.find_elements(By.CSS_SELECTOR, ".aod-information-block")
        texts = [self.driver.execute_script("return arguments[0].innerText;", b) or "" for b in blocks]
        if any("Stock" in t for t in texts):
            log.info("Keepa stock data ready (%d blocks)", len(blocks))
        elif any("Sold by" in t for t in texts):
            log.warning("Seller names loaded but no Keepa stock (Keepa may not be logged in)")
        else:
            log.warning("AOD panel content not visible after %ds", wait_seconds)

    def _is_captcha_page(self) -> bool:
        # Check URL and page-level indicators only — avoid false positives from browser extensions
        url = self.driver.current_url.lower()
        if "captcha" in url or "validatecaptcha" in url:
            return True
        try:
            title = self.driver.title.lower()
            if "robot check" in title or "captcha" in title:
                return True
            # Look for the actual CAPTCHA input form, not just the word "captcha" in source
            self.driver.find_element(By.CSS_SELECTOR, "form[action*='validateCaptcha'] input")
            return True
        except NoSuchElementException:
            return False

    def load_product_page(self, asin: str, retry: int = 0) -> bool:
        # th=1&psc=1 forces a default variation to be selected so the AOD panel shows real sellers
        url = f"https://www.amazon.com/dp/{asin}?th=1&psc=1"
        try:
            self.driver.get(url)
            self.wait.until(EC.presence_of_element_located((By.ID, "productTitle")))
            human_delay((3, 5))
            log.info("Product page loaded: %s", asin)
            return True
        except (TimeoutException, WebDriverException) as e:
            if "no such execution context" in str(e) or "invalid session" in str(e):
                if retry < 2:
                    log.warning("Frame context lost for %s — retrying (%d/2)", asin, retry + 1)
                    time.sleep(5)
                    return self.load_product_page(asin, retry=retry + 1)
                log.error("Frame context lost after retries — skipping %s", asin)
                return False
            if self._is_captcha_page():
                if retry >= 2:
                    log.error("CAPTCHA persisted after %d retries — skipping %s", retry, asin)
                    return False
                log.warning("CAPTCHA detected for %s — waiting 60s then retrying (attempt %d/2)", asin, retry + 1)
                time.sleep(60)
                return self.load_product_page(asin, retry=retry + 1)
            log.error("Timeout loading product page for %s (URL: %s)", asin, self.driver.current_url)
            return False
        except WebDriverException as e:
            err = str(e)
            if "no such execution context" in err or "invalid session" in err:
                if retry < 2:
                    log.warning("Frame context lost for %s — retrying (%d/2)", asin, retry + 1)
                    time.sleep(5)
                    return self.load_product_page(asin, retry=retry + 1)
            log.error("WebDriver error on product page %s: %s", asin, e)
            return False

    def open_offers_panel(self, asin: str) -> bool:
        """
        Click the 'See All Buying Options' / 'Other Sellers' link on the product page
        to open the AOD side panel. Never navigates away to a separate URL.
        """
        # CSS selectors to try
        css_selectors = [
            "#aod-ingress-link",
            "#buybox-see-all-buying-choices",
            "#new-buybox-see-all-buying-choices-announce",
            "[data-action='show-all-offers-display']",
            "#olp-upd-new",
            "a[href*='aod'][href*='condition']",
        ]
        for sel in css_selectors:
            try:
                link = self.driver.find_element(By.CSS_SELECTOR, sel)
                self.driver.execute_script("arguments[0].scrollIntoView(true);", link)
                time.sleep(1)
                self.driver.execute_script("arguments[0].click();", link)
                self.wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#aod-offer, #aod-container, #aod-price-1")
                ))
                self._wait_for_keepa_injection()
                log.info("Opened offers panel via CSS '%s' for %s", sel, asin)
                return True
            except (NoSuchElementException, TimeoutException):
                continue

        # Try finding by visible link text
        for phrase in ["See All Buying Options", "Other Sellers on Amazon",
                       "See all buying options", "buying options"]:
            try:
                link = self.driver.find_element(By.PARTIAL_LINK_TEXT, phrase)
                self.driver.execute_script("arguments[0].scrollIntoView(true);", link)
                time.sleep(1)
                self.driver.execute_script("arguments[0].click();", link)
                self.wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#aod-offer, #aod-container, #aod-price-1")
                ))
                self._wait_for_keepa_injection()
                log.info("Opened offers panel via link text '%s' for %s", phrase, asin)
                return True
            except (NoSuchElementException, TimeoutException):
                continue

        # Log what's on the page to help diagnose
        log.warning("Could not find 'See All Buying Options' for %s — logging page elements", asin)
        for tag in ["a", "button", "span"]:
            for el in self.driver.find_elements(By.TAG_NAME, tag)[:200]:
                txt = (el.text or "").strip()
                idd = el.get_attribute("id") or ""
                if any(k in (txt + idd).lower() for k in ["buying", "offer", "seller", "aod"]):
                    log.warning("  Found <%s> id=%r text=%r", tag, idd, txt[:80])
        return False

    def load_offers_page(self, asin: str, page: int = 1) -> bool:
        """Direct navigation to the AOD offers page (fallback or page 2+)."""
        urls = [
            (f"https://www.amazon.com/gp/aod/ajax"
             f"?asin={asin}&pc=dp&isonlyrenderofferlist=true"
             f"&pageno={page}&condition=new"),
            (f"https://www.amazon.com/gp/product/ajax"
             f"?asin={asin}&pc=dp&isonlyrenderofferlist=true"
             f"&pageno={page}&condition=new"),
        ]
        for url in urls:
            try:
                self.driver.get(url)
                self.wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#aod-offer, #aod-container, #aod-price-1")
                ))
                human_delay((2, 4))
                return True
            except TimeoutException:
                continue
        log.warning("Offer page timeout (page %d) for %s", page, asin)
        return False

    # ── Product page data ──────────────────────────
    def get_title(self) -> Optional[str]:
        try:
            el = self.driver.find_element(By.ID, "productTitle")
            return el.text.strip()
        except NoSuchElementException:
            return None

    def get_brand(self) -> Optional[str]:
        for selector in ["#bylineInfo", "#brand", ".po-brand .a-span9"]:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, selector)
                text = el.text.strip()
                return re.sub(r"^(Visit the |Brand: )", "", text).replace(" Store", "")
            except NoSuchElementException:
                continue
        return None

    def get_variation_info(self) -> dict:
        """
        Extract parent ASIN, variation size and color from the product page.
        Reads the "Color: X" and "Size: X" labels Amazon renders on the twister widget.
        """
        result = {}
        try:
            page_source = self.driver.page_source

            # Parent ASIN from embedded JSON
            m = re.search(r'"parentAsin"\s*:\s*"([A-Z0-9]{10})"', page_source)
            if m:
                result["parent_asin"] = m.group(1)

            # Size + Color — read from the inline twister expanded dimension text elements.
            # These IDs are confirmed present on Amazon product pages:
            #   #inline-twister-expanded-dimension-text-size_name  → "Large"
            #   #inline-twister-expanded-dimension-text-color_name → "Green"
            size, color = self.driver.execute_script("""
                function get(id) {
                    var el = document.getElementById(id);
                    return el ? (el.innerText || el.textContent || '').trim() : null;
                }
                return [
                    get('inline-twister-expanded-dimension-text-size_name'),
                    get('inline-twister-expanded-dimension-text-color_name')
                ];
            """)
            if size:
                result["variation_size"] = size
            if color:
                result["variation_color"] = color

            log.info("Variation: parent=%s size=%s color=%s",
                     result.get("parent_asin"), result.get("variation_size"), result.get("variation_color"))
        except Exception as e:
            log.debug("get_variation_info error: %s", e)
        return result

    def get_bsr(self) -> dict:
        """
        Extract BSR from Amazon's product details table.
        Returns {"rank": int, "category": str}
        Keepa also overlays this data — we read Amazon's native first,
        then check Keepa's injected panel for confirmation.
        """
        result = {}

        # Strategy 1: product details bullets
        selectors = [
            "#detailBulletsWrapper_feature_div",
            "#productDetails_detailBullets_sections1",
            "#productDetails_db_sections",
            "#prodDetails",
            "#productDetails_feature_div",   # child ASIN product details table
            ".a-section.a-spacing-medium.a-spacing-top-small",  # alternate table
        ]
        for sel in selectors:
            try:
                container = self.driver.find_element(By.CSS_SELECTOR, sel)
                # Use innerText so off-screen/hidden sections are readable
                text = self.driver.execute_script("return arguments[0].innerText;", container) or ""
                m = re.search(r"#([\d,]+)\s+in\s+([^\n\(]+)", text)
                if m:
                    result["rank"]     = int(m.group(1).replace(",", ""))
                    result["category"] = m.group(2).strip()
                    log.info("BSR from product details: #%d in %s",
                             result["rank"], result["category"])
                    return result
            except NoSuchElementException:
                continue

        # Strategy 1b: scan entire page text for BSR pattern
        try:
            body_text = self.driver.execute_script("return document.body.innerText;") or ""
            m = re.search(r"Best Sellers Rank[:\s]+#([\d,]+)\s+in\s+([^\n\(]+)", body_text)
            if m:
                result["rank"]     = int(m.group(1).replace(",", ""))
                result["category"] = m.group(2).strip()
                log.info("BSR from page scan: #%d in %s", result["rank"], result["category"])
                return result
        except Exception:
            pass

        # Strategy 2: Keepa-injected BSR label
        try:
            keepa_bsr = self.driver.find_element(
                By.CSS_SELECTOR, "#keepa-container .keepa-rank, .keepaBSR"
            )
            m = re.search(r"#([\d,]+)", keepa_bsr.text)
            if m:
                result["rank"] = int(m.group(1).replace(",", ""))
                log.info("BSR from Keepa panel: #%d", result["rank"])
                return result
        except NoSuchElementException:
            pass

        log.warning("Could not extract BSR from page")
        return result

    # ── Offers page data ──────────────────────────
    def get_sellers_from_offers_page(self) -> list[dict]:
        """
        Parse the AOD panel for all sellers.
        Amazon's current DOM uses:
          - #aod-pinned-offer  → buy-box seller
          - #aod-offer-list .aod-information-block  → other sellers
        Each block contains soldBy, shipsFrom, price, rating sub-divs.
        """
        sellers = []

        try:
            self.wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#aod-pinned-offer, #aod-offer-list, .aod-information-block")
            ))
        except TimeoutException:
            log.warning("No offer blocks appeared in AOD panel")
            return sellers

        # Pinned / buy-box offer
        try:
            pinned = self.driver.find_element(By.CSS_SELECTOR, "#aod-pinned-offer")
            merged_text = self._expand_see_more(pinned)
            s = self._parse_offer_block(pinned, is_buy_box=True, merged_text=merged_text)
            if s:
                sellers.append(s)
        except NoSuchElementException:
            pass

        # Listed offers
        try:
            offer_list = self.driver.find_element(By.CSS_SELECTOR, "#aod-offer-list")
            blocks = offer_list.find_elements(By.CSS_SELECTOR, ".aod-information-block")
            log.info("Found %d offer blocks", len(blocks))
            for block in blocks:
                merged_text = self._expand_see_more(block)
                s = self._parse_offer_block(block, is_buy_box=False, merged_text=merged_text)
                if s:
                    sellers.append(s)
        except NoSuchElementException:
            log.warning("No #aod-offer-list found")

        return sellers

    def _expand_see_more(self, block) -> str:
        """
        For buy-box blocks, Stock is visible before 'See more' and seller name after.
        Read text before clicking, click, read again, return combined text so both
        stock and seller name are available for parsing.
        """
        text_before = self.driver.execute_script("return arguments[0].innerText;", block) or ""
        try:
            see_more = block.find_element(
                By.XPATH,
                ".//a[contains(normalize-space(.), 'See more')] | "
                ".//span[contains(@class,'a-declarative') and contains(., 'See more')]"
            )
            self.driver.execute_script("arguments[0].click();", see_more)
            time.sleep(0.6)
        except NoSuchElementException:
            return text_before
        except Exception as e:
            log.debug("_expand_see_more: %s", e)
            return text_before
        text_after = self.driver.execute_script("return arguments[0].innerText;", block) or ""
        # Merge: keep both so regex can find Stock from before and Sold by from after
        return text_before + "\n" + text_after

    def _parse_offer_block(self, block, is_buy_box: bool = False, merged_text: str = "") -> Optional[dict]:
        """
        Parse one seller offer block from Amazon's AOD panel.
        merged_text: pre+post 'See more' text combined — contains both Stock and Sold by.
        """
        try:
            seller: dict = {"is_buy_box": is_buy_box}
            # Use merged_text (before+after See more click) so Stock and seller name are both present
            block_text = merged_text or (self.driver.execute_script("return arguments[0].innerText;", block) or "")

            # ── Price ──
            for price_sel in [".a-price .a-offscreen", ".a-price-whole"]:
                try:
                    el = block.find_element(By.CSS_SELECTOR, price_sel)
                    raw = re.sub(r"[^\d.]", "", (el.get_attribute("textContent") or el.text or "").strip())
                    if raw:
                        seller["price"] = float(raw)
                        break
                except (NoSuchElementException, ValueError):
                    continue

            if "price" not in seller:
                return None

            # ── Ships from / FBA — parse from block text (most reliable) ──
            m = re.search(r"Ships from\s*\n(.+)", block_text)
            ships_from_text = m.group(1).strip().lower() if m else ""
            is_fba = "amazon" in ships_from_text
            seller["fulfillment"] = "FBA" if is_fba else "FBM"

            # ── Seller name — parse from block text ──
            m = re.search(r"Sold by\s*\n(.+)", block_text)
            if m:
                raw = m.group(1).strip()
                seller["seller_name"] = re.sub(r"\s*\(\d+.*", "", raw).strip()
                # Also grab seller URL from element (can't get href from text)
                try:
                    sold_el = block.find_element(By.XPATH, ".//*[contains(@id,'soldBy')]//a")
                    seller["seller_url"] = sold_el.get_attribute("href") or ""
                except Exception:
                    pass
            elif "amazon" in block_text.lower() and is_fba:
                seller["seller_name"] = "Amazon.com"
            else:
                seller["seller_name"] = "Unknown"

            # ── Positive % — parse from block text ──
            m = re.search(r"(\d+)%\s+positive", block_text)
            if m:
                seller["positive_pct"] = float(m.group(1))

            # ── Inventory (Keepa injects "Stock\nN" or "Stock\n30+" at block end) ──
            # Keepa Pro format: the word "Stock" followed by newline then the count
            m = re.search(r"\bStock\s*\n?\s*:?\s*(\d+)\+?", block_text)
            if m:
                seller["inventory"] = int(m.group(1))
            else:
                # Amazon native "Only X left in stock"
                m = re.search(r"[Oo]nly\s+(\d+)\s+left", block_text)
                if m:
                    seller["inventory"] = int(m.group(1))
                else:
                    seller["inventory"] = None

            # ── Shipping cost ──
            if "FREE" in block_text or "free" in block_text:
                seller["shipping"] = 0.0
            else:
                m = re.search(r"\+\s*\$([\d.]+)\s+shipping", block_text, re.IGNORECASE)
                seller["shipping"] = float(m.group(1)) if m else 0.0

            log.info("Parsed: %-30s | %s | $%.2f | inv=%s",
                     seller.get("seller_name"), seller.get("fulfillment"),
                     seller.get("price", 0), seller.get("inventory"))
            return seller

        except Exception as e:
            log.debug("Failed to parse offer block: %s", e)
            return None

    def get_total_offer_pages(self) -> int:
        """Check if there are multiple pages of offers"""
        try:
            pagination = self.driver.find_element(
                By.CSS_SELECTOR, ".a-pagination, #aod-pagination"
            )
            pages = pagination.find_elements(By.TAG_NAME, "li")
            return max(len(pages) - 1, 1)
        except NoSuchElementException:
            return 1

    def scroll_to_bottom(self):
        """Scroll page to trigger lazy-loaded content"""
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
        human_delay(DELAY_AFTER_SCROLL)
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        human_delay(DELAY_AFTER_SCROLL)


# ──────────────────────────────
# DATABASE (same as before)
# ──────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema", "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with get_conn() as conn:
        conn.executescript(sql)
    log.info("DB ready: %s", DB_PATH)


def upsert_product(conn, asin, title=None, brand=None,
                   parent_asin=None, variation_size=None, variation_color=None):
    conn.execute("""
        INSERT INTO dim_product (asin, parent_asin, title, brand, variation_size, variation_color)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asin) DO UPDATE SET
            title           = COALESCE(excluded.title, title),
            brand           = COALESCE(excluded.brand, brand),
            parent_asin     = COALESCE(excluded.parent_asin, parent_asin),
            variation_size  = COALESCE(excluded.variation_size, variation_size),
            variation_color = COALESCE(excluded.variation_color, variation_color)
    """, (asin, parent_asin, title, brand, variation_size, variation_color))


def calculate_and_save(conn, asin: str, today: str, sellers: list,
                       bsr_rank, bsr_category: str):
    """
    Build asin_daily row: aggregate seller stats, calculate units sold vs yesterday.
    sellers: list of dicts with keys name, fulfillment, price, inventory
    """
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    # Load yesterday's sellers from JSON
    y_row = conn.execute(
        "SELECT sellers FROM fct_asin_daily WHERE asin=? AND snapshot_date=?",
        (asin, yesterday)
    ).fetchone()
    y_sellers = json.loads(y_row["sellers"]) if y_row and y_row["sellers"] else []
    y_map = {s["name"]: s.get("inventory") for s in y_sellers}

    # Calculate units sold per seller
    units_total = fba_units = fbm_units = 0
    if y_map:
        t_map = {s["name"]: s for s in sellers}
        for name, inv_y in y_map.items():
            if inv_y is None:
                continue
            t_seller = t_map.get(name)
            inv_t = t_seller.get("inventory") if t_seller else None
            if inv_t is None:
                sold = inv_y  # seller gone → all remaining inventory sold
            else:
                sold = max(0, inv_y - inv_t)
            if sold:
                fulfillment = y_sellers[[s["name"] for s in y_sellers].index(name)].get("fulfillment", "FBM")
                if fulfillment == "FBA":
                    fba_units += sold
                else:
                    fbm_units += sold
                units_total += sold
        log.info("Units sold: %s date=%s → %d units", asin, yesterday, units_total)
    else:
        log.info("No yesterday data for %s — units_sold=NULL (day 1)", asin)
        units_total = fba_units = fbm_units = None

    # Aggregate seller stats
    valid = [s for s in sellers if s.get("price") is not None]
    total_inv = sum(s.get("inventory") or 0 for s in valid)
    wavg = (
        sum(s["price"] * (s.get("inventory") or 1) for s in valid) /
        sum(s.get("inventory") or 1 for s in valid)
    ) if valid else None
    min_p = min(s["price"] for s in valid) if valid else None
    max_p = max(s["price"] for s in valid) if valid else None
    fba_cnt = sum(1 for s in sellers if s.get("fulfillment") == "FBA")
    fbm_cnt = sum(1 for s in sellers if s.get("fulfillment") == "FBM")

    sellers_json = json.dumps([
        {"name": s.get("name") or s.get("seller_name"),
         "fulfillment": s.get("fulfillment", "FBM"),
         "price": s.get("price"),
         "inventory": s.get("inventory")}
        for s in sellers
    ])

    conn.execute("""
        INSERT OR REPLACE INTO fct_asin_daily
            (snapshot_date, asin, bsr_rank, bsr_category, sellers,
             total_sellers, fba_sellers, fbm_sellers,
             min_price, max_price, avg_price_weighted, total_inventory,
             units_sold, fba_units_sold, fbm_units_sold)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (today, asin, bsr_rank, bsr_category, sellers_json,
          len(sellers), fba_cnt, fbm_cnt,
          min_p, max_p, wavg, total_inv,
          units_total, fba_units, fbm_units))


# ──────────────────────────────
# MAIN PIPELINE
# ──────────────────────────────
def run_pipeline_for_asin(asin: str, parser: AmazonPageParser):
    today = date.today().isoformat()
    hour  = datetime.now().hour
    log.info("── ASIN: %s ──", asin)

    # 1. Load product page
    if not parser.load_product_page(asin):
        log.error("Skipping %s — product page failed to load", asin)
        return

    parser.scroll_to_bottom()   # trigger Keepa injection

    # 2. Extract product info + BSR + variation
    title    = parser.get_title()
    brand    = parser.get_brand()
    bsr_data = parser.get_bsr()
    var_info = parser.get_variation_info()
    bsr_rank = bsr_data.get("rank")
    bsr_cat  = bsr_data.get("category", "Unknown")

    # Standalone listing: parent_asin == asin (or missing) — no variation widget.
    # Fall back to extracting size/color from the title.
    parent = var_info.get("parent_asin")
    is_standalone = not parent or parent == asin
    if is_standalone and not var_info.get("variation_size") and not var_info.get("variation_color"):
        title_info = extract_variation_from_title(title)
        if title_info:
            var_info.update(title_info)
            log.info("Standalone listing — size/color extracted from title: %s / %s",
                     title_info.get("variation_size"), title_info.get("variation_color"))

    log.info("Title: %s | Brand: %s | BSR: %s | Parent: %s | Size: %s | Color: %s",
             title, brand, bsr_rank,
             var_info.get("parent_asin"), var_info.get("variation_size"), var_info.get("variation_color"))

    human_delay(DELAY_BETWEEN_PAGES)

    # 3. Open the offers panel and collect all sellers
    all_sellers = []

    if parser.open_offers_panel(asin):
        page_sellers = parser.get_sellers_from_offers_page()
        all_sellers.extend(page_sellers)
        total_pages = parser.get_total_offer_pages()
        log.info("Offers page 1/%d: %d sellers found", total_pages, len(page_sellers))

        # Page 2+ — scroll down in the already-open AOD panel to load more sellers
        for page_num in range(2, total_pages + 1):
            human_delay(DELAY_BETWEEN_PAGES)
            try:
                # Scroll to bottom of AOD container to trigger next page load
                aod = parser.driver.find_element(By.CSS_SELECTOR, "#aod-container, #aod-offer-list")
                parser.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", aod)
                time.sleep(4)
            except Exception:
                break
            page_sellers = parser.get_sellers_from_offers_page()
            new_sellers = [s for s in page_sellers if s not in all_sellers]
            if not new_sellers:
                break
            all_sellers.extend(new_sellers)
            log.info("Offers page %d/%d: %d additional sellers", page_num, total_pages, len(new_sellers))

    log.info("Total sellers collected: %d for %s", len(all_sellers), asin)

    # 4. Save to DB
    init_db()
    with get_conn() as conn:
        upsert_product(conn, asin, title=title, brand=brand,
                       parent_asin=var_info.get("parent_asin"),
                       variation_size=var_info.get("variation_size"),
                       variation_color=var_info.get("variation_color"))

        # Normalise + deduplicate: keep entry with inventory when same seller appears twice
        seen: dict = {}
        for s in all_sellers:
            name = s.get("seller_name") or s.get("name") or "Unknown"
            key = (name, s.get("fulfillment", "FBM"))
            existing = seen.get(key)
            if existing is None or (s.get("inventory") is not None and existing.get("inventory") is None):
                seen[key] = {**s, "name": name}
        sellers_clean = list(seen.values())

        calculate_and_save(conn, asin, today, sellers_clean, bsr_rank, bsr_cat)
        conn.commit()

    log.info("Saved: %s | %d sellers | BSR #%s", asin, len(all_sellers), bsr_rank)


def _keepa_is_logged_in(driver: webdriver.Chrome) -> bool:
    """Return True if the current keepa.com page shows a logged-in session."""
    src = driver.page_source.lower()
    # Keepa shows '#myAccount' link or account icon when logged in
    return any(k in src for k in ["#myaccount", "logout", "sign out", "log out", "my account",
                                   "account-nav", "keepa-user"])


def _ensure_keepa_logged_in(driver: webdriver.Chrome):
    """
    Navigate to keepa.com and ensure Keepa is authenticated.
    If credentials are in .env, auto-logs in silently.
    Otherwise waits up to 3 minutes for manual login.
    The .chrome_profile saves the session so this only runs once.
    """
    driver.get("https://keepa.com/#!")
    time.sleep(6)

    if _keepa_is_logged_in(driver):
        log.info("Keepa already logged in — continuing")
        return

    # Try auto-login with credentials from .env
    if KEEPA_USERNAME and KEEPA_PASSWORD:
        log.info("Auto-logging into Keepa as '%s'...", KEEPA_USERNAME)
        try:
            wait = WebDriverWait(driver, 20)

            # Keepa uses a hash-based SPA — the login form appears after clicking a nav item.
            # Try clicking any login-related element
            for login_sel in [
                "a[href*='login']", "a[href='#!login']",
                "[onclick*='login']", ".loginButton",
                "a.nav-link[href*='login']",
            ]:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, login_sel)
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(3)
                    break
                except Exception:
                    continue

            # Try navigating directly to the login hash
            if not driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
                driver.get("https://keepa.com/#!login")
                time.sleep(4)

            # Fill username — Keepa's form has input fields for email and password
            for user_sel in ["input[name='username']", "input[type='email']",
                             "input[type='text']", "#username", "#email"]:
                try:
                    user_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, user_sel)))
                    user_field.clear()
                    user_field.send_keys(KEEPA_USERNAME)
                    break
                except Exception:
                    continue

            # Fill password
            for pass_sel in ["input[type='password']", "input[name='password']", "#password"]:
                try:
                    pass_field = driver.find_element(By.CSS_SELECTOR, pass_sel)
                    pass_field.clear()
                    pass_field.send_keys(KEEPA_PASSWORD)
                    break
                except Exception:
                    continue

            # Submit
            for submit_sel in ["button[type='submit']", "input[type='submit']",
                               ".btn-primary", "button.login", "#loginBtn", "form button"]:
                try:
                    driver.find_element(By.CSS_SELECTOR, submit_sel).click()
                    break
                except Exception:
                    continue

            time.sleep(6)

            if _keepa_is_logged_in(driver):
                log.info("Keepa auto-login successful")
                return
            else:
                log.warning("Auto-login attempted but not confirmed — will wait for manual login")
        except Exception as e:
            log.warning("Auto-login failed: %s", e)

    # Manual fallback
    log.info("═══ Please log into Keepa manually in the browser window ═══")
    log.info("Waiting up to 3 minutes...")
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(5)
        if _keepa_is_logged_in(driver):
            log.info("Keepa login confirmed — continuing")
            return

    log.warning("Keepa not logged in — stock data will be missing for this run")


CHECKPOINT_FILE = Path(__file__).parent / ".tracker_checkpoint.json"
CHROME_RESTART_EVERY = 200  # restart browser after this many ASINs to free memory


def _load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_checkpoint(run_date: str, completed: list):
    CHECKPOINT_FILE.write_text(json.dumps({"date": run_date, "completed": completed}))


def _clear_checkpoint():
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


def _start_driver(connect_port: int) -> webdriver.Chrome:
    if connect_port:
        return create_driver_cdp(connect_port)
    return create_driver(headless=False)


def run_all(asins: list, connect_port: int = 0):
    today = date.today().isoformat()
    log.info("═══ Run started: %d ASINs | %s ═══", len(asins), today)

    # Resume from checkpoint if same day
    checkpoint  = _load_checkpoint()
    completed   = checkpoint.get("completed", []) if checkpoint.get("date") == today else []
    if completed:
        log.info("Resuming from checkpoint — %d ASINs already done today", len(completed))

    remaining = [a for a in asins if a.strip() not in completed]
    log.info("%d ASINs remaining", len(remaining))

    driver = _start_driver(connect_port)

    if connect_port:
        # CDP mode: user's Chrome is already set up — just verify Keepa, don't navigate away
        driver.get("https://keepa.com/#!")
        time.sleep(5)
        if _keepa_is_logged_in(driver):
            log.info("Keepa confirmed logged in")
        else:
            log.warning("Keepa NOT logged in — stock data will be missing. "
                        "Please log into keepa.com in the browser window, then re-run.")
    else:
        _ensure_keepa_logged_in(driver)

    parser = AmazonPageParser(driver)
    batch_count = 0

    try:
        for i, asin in enumerate(remaining):
            asin = asin.strip()

            # ── Restart Chrome every N ASINs to clear memory ──
            if batch_count > 0 and batch_count % CHROME_RESTART_EVERY == 0:
                log.info("── Restarting Chrome after %d ASINs (memory reset) ──", batch_count)
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(5)
                driver = _start_driver(connect_port)
                if not connect_port:
                    _ensure_keepa_logged_in(driver)
                parser = AmazonPageParser(driver)
                log.info("Chrome restarted successfully")

            try:
                run_pipeline_for_asin(asin, parser)
                completed.append(asin)
                _save_checkpoint(today, completed)
                batch_count += 1
            except Exception as e:
                import traceback
                log.error("Error processing %s: %s\n%s", asin, e, traceback.format_exc())

            if i < len(remaining) - 1:
                human_delay(DELAY_BETWEEN_ASINS)

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        log.info("Browser closed.")

    _clear_checkpoint()
    log.info("═══ Run complete: %d/%d ASINs processed ═══", len(completed), len(asins))


# ──────────────────────────────
# SCHEDULER
# ──────────────────────────────
def start_scheduler(asins: list, interval_hours: int = 6, connect_port: int = 0):
    log.info("Scheduler: every %dh | %d ASINs", interval_hours, len(asins))

    def job():
        try:
            run_all(asins, connect_port=connect_port)
        except Exception as e:
            log.error("Scheduled run failed: %s", e)

    schedule.every(interval_hours).hours.do(job)
    job()   # run now

    while True:
        schedule.run_pending()
        time.sleep(60)


# ──────────────────────────────
# CLI
# ──────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amazon Tracker — Browser (Selenium + Keepa)")
    parser.add_argument("--asin",      default="B0CV3CDPTK", help="Single ASIN to track")
    parser.add_argument("--asins",     help="Comma-separated ASINs, e.g. B001,B002,B003")
    parser.add_argument("--asins-file",help="Path to .txt file, one ASIN per line")
    parser.add_argument("--scheduler",    action="store_true", help="Run on a schedule")
    parser.add_argument("--interval",     type=int, default=6, help="Hours between runs (default 6)")
    parser.add_argument("--connect-port", type=int, default=0,
                        help="Connect to already-running Chrome on this CDP port (e.g. 9222). "
                             "Skips login — use when Chrome + Keepa are already logged in.")
    args = parser.parse_args()

    # Build ASIN list from whichever source was given
    if args.asins_file:
        with open(args.asins_file) as f:
            asin_list = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.asins:
        asin_list = [a.strip() for a in args.asins.split(",")]
    else:
        asin_list = [args.asin]

    if args.scheduler:
        start_scheduler(asin_list, interval_hours=args.interval, connect_port=args.connect_port)
    else:
        run_all(asin_list, connect_port=args.connect_port)
