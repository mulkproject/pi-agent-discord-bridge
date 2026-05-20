#!/usr/bin/env python3
"""
Screenshot tool — called by pi agent via bash.
Saves screenshot to a temp file and prints the path.

Usage:
    python3 screenshot.py https://example.com [output.png]
"""

import sys
import os
import tempfile

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ Playwright not installed. Run: pip install playwright && python3 -m playwright install chromium")
    sys.exit(1)

if len(sys.argv) < 2:
    print("Usage: screenshot.py <url> [output.png]")
    sys.exit(1)

url = sys.argv[1]
if not url.startswith(("http://", "https://")):
    url = "https://" + url

output_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(tempfile.mkdtemp(), "screenshot.png")

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.screenshot(path=output_path, full_page=False)
        browser.close()

    print(f"✅ Screenshot saved: {output_path}")
except Exception as e:
    print(f"❌ Failed: {e}")
    sys.exit(1)
