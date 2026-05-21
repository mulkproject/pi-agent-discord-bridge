#!/usr/bin/env python3
"""
Screenshot tool — called by pi agent via bash.
Saves screenshot to a temp file and prints the path.
Supports optional login and element removal before taking the screenshot.

Usage:
    python3 screenshot.py <url> [output.png]
    python3 screenshot.py <url> [output.png] --login --email <email> --password <pass>
    python3 screenshot.py <url> [output.png] --login --email <email> --password <pass> --remove "TERMS,DEMO"
"""

import sys
import os
import tempfile
import argparse
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ Playwright not installed. Run: pip install playwright && python3 -m playwright install chromium")
    sys.exit(1)

parser = argparse.ArgumentParser(description="Take a screenshot of a webpage.")
parser.add_argument("url", help="URL to screenshot")
parser.add_argument("output", nargs="?", help="Output file path")
parser.add_argument("--login", action="store_true", help="Login before taking screenshot")
parser.add_argument("--email", help="Login email")
parser.add_argument("--password", help="Login password")
parser.add_argument("--remove", help="Comma-separated text of elements to hide (e.g. 'TERMS,DEMO')")
args = parser.parse_args()

url = args.url
if not url.startswith(("http://", "https://")):
    url = "https://" + url

output_path = args.output if args.output else os.path.join(tempfile.mkdtemp(), "screenshot.png")

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        if args.login and args.email and args.password:
            parsed = urlparse(url)
            login_url = f"{parsed.scheme}://{parsed.netloc}/login"
            print(f"🔐 Logging in at {login_url}...")
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            page.fill('input[type="email"], input[name="email"]', args.email)
            page.fill('input[type="password"], input[name="password"]', args.password)
            page.click('button[type="submit"], button:has-text("Login"), button:has-text("Sign In")')
            page.wait_for_timeout(3000)

        print(f"📸 Navigating to {url}...")
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # Remove specified elements by text content
        if args.remove:
            items = [item.strip() for item in args.remove.split(",")]
            for item in items:
                # Try to find and hide elements containing that text
                script = f"""
                () => {{
                    const items = document.querySelectorAll('*');
                    const text = '{item}';
                    let removed = 0;
                    items.forEach(el => {{
                        if (el.children.length === 0 && el.textContent.trim() === text) {{
                            el.style.display = 'none';
                            removed++;
                        }} else if (el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'SPAN' || el.tagName === 'DIV') {{
                            if (el.textContent.trim() === text && el.children.length <= 1) {{
                                el.style.display = 'none';
                                removed++;
                            }}
                        }}
                    }});
                    // Also try finding by partial match for tab-like elements
                    document.querySelectorAll('a, button, span, li, div.tab, [class*="tab"], [class*="nav-item"]').forEach(el => {{
                        if (el.textContent.trim().toUpperCase() === text.toUpperCase()) {{
                            el.style.display = 'none';
                            removed++;
                        }}
                    }});
                    return removed;
                }}
                """
                count = page.evaluate(script)
                print(f"   🗑️ Hid {count} element(s) matching '{item}'")

        page.wait_for_timeout(500)
        page.screenshot(path=output_path, full_page=True)
        browser.close()

    print(f"✅ Screenshot saved: {output_path}")
except Exception as e:
    print(f"❌ Failed: {e}")
    sys.exit(1)
