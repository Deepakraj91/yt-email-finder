import re
import time
import requests
import io
import csv
import os
from typing import Optional
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
BLOCKED_DOMAINS = ['youtube', 'google', 'gstatic', 'w3.org', 'schema',
                   'example', 'sentry', 'googleapis', 'cloudflare', 'yt3']

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0.0.0 Safari/537.36'),
    'Accept-Language': 'en-US,en;q=0.9'
}


def clean_handle(handle: str) -> str:
    handle = handle.strip()
    if handle and not handle.startswith('@'):
        handle = '@' + handle
    return handle


def _filter_emails(emails):
    return [
        e for e in emails
        if not any(bad in e.lower() for bad in BLOCKED_DOMAINS)
    ]


# ---------- Method 1: fast requests-based scrape ----------

def scrape_email_requests(handle: str) -> Optional[str]:
    url = f'https://www.youtube.com/{handle}/about'
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        # Try description simpleText fields
        desc_matches = re.findall(r'"description":\{"simpleText":"(.*?)"', resp.text)
        desc_text = ' '.join(desc_matches)
        emails = _filter_emails(EMAIL_PATTERN.findall(desc_text))
        if emails:
            return emails[0]

        # Try businessEmail field
        biz = re.search(
            r'businessEmail[\":\s]+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
            resp.text
        )
        if biz:
            return biz.group(1)

        # Full-page raw scan
        emails = _filter_emails(EMAIL_PATTERN.findall(resp.text))
        return emails[0] if emails else None

    except Exception:
        return None


# ---------- Method 2: Playwright — click "View email address" ----------

def scrape_email_playwright(handle: str) -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception:
        return None

    url = f'https://www.youtube.com/{handle}/about'

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                ]
            )
        except Exception:
            return None
        context = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            locale='en-US',
            viewport={'width': 1280, 'height': 800},
        )

        # Hide automation fingerprints
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()
        email_found = None

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=20000)
            page.wait_for_timeout(2500)

            # ---- Step 1: click "more" if present to expand description ----
            more_btn = page.locator('tp-yt-paper-button#expand, yt-description-preview-view-model button, #description-container tp-yt-paper-button').first
            if more_btn.count() > 0:
                try:
                    more_btn.click(timeout=3000)
                    page.wait_for_timeout(1000)
                except PWTimeout:
                    pass

            # ---- Step 2: look for "View email address" button ----
            view_email_btn = page.locator(
                'text="View email address", '
                'yt-channel-external-link-view-model a[href*="mailto"], '
                '#email-container button, '
                'button:has-text("View email address")'
            ).first

            if view_email_btn.count() > 0:
                view_email_btn.click(timeout=5000)
                page.wait_for_timeout(2000)

                # ---- Step 3: handle CAPTCHA dialog if it appears ----
                # YouTube shows a reCAPTCHA checkbox — try to tick it
                captcha_frame = page.frame_locator('iframe[src*="recaptcha"]').first
                if captcha_frame:
                    try:
                        captcha_frame.locator('#recaptcha-anchor').click(timeout=4000)
                        page.wait_for_timeout(3000)
                    except PWTimeout:
                        pass

                # ---- Step 4: click Submit / Verify button ----
                for btn_text in ['Submit', 'Verify', 'Continue', 'Done']:
                    submit = page.locator(f'button:has-text("{btn_text}")').first
                    if submit.count() > 0:
                        try:
                            submit.click(timeout=3000)
                            page.wait_for_timeout(2000)
                            break
                        except PWTimeout:
                            pass

                # ---- Step 5: extract revealed email from page ----
                page_text = page.inner_text('body')
                emails = _filter_emails(EMAIL_PATTERN.findall(page_text))
                if emails:
                    email_found = emails[0]

            # ---- Fallback: scan full rendered page text ----
            if not email_found:
                page_text = page.inner_text('body')
                emails = _filter_emails(EMAIL_PATTERN.findall(page_text))
                if emails:
                    email_found = emails[0]

            # ---- Fallback: check mailto links ----
            if not email_found:
                mailto = page.locator('a[href^="mailto:"]').first
                if mailto.count() > 0:
                    href = mailto.get_attribute('href', timeout=2000) or ''
                    email_found = href.replace('mailto:', '').strip() or None

        except Exception:
            pass
        finally:
            browser.close()

        return email_found


# ---------- Combined scraper: fast first, browser fallback ----------

def scrape_email(handle: str) -> Optional[str]:
    # Try fast method first
    email = scrape_email_requests(handle)
    if email:
        return email

    # Fall back to Playwright for channels with hidden/CAPTCHA-gated emails
    return scrape_email_playwright(handle)


# ---------- Flask routes ----------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/find-emails', methods=['POST'])
def find_emails():
    try:
        data = request.get_json(force=True, silent=True) or {}
        raw_handles = data.get('handles', '')
        handles = [clean_handle(h) for h in raw_handles.splitlines() if h.strip()]
        handles = list(dict.fromkeys(handles))  # deduplicate, preserve order

        results = []
        for handle in handles:
            try:
                email = scrape_email(handle)
            except Exception:
                email = None
            results.append({
                'handle': handle,
                'email': email or '',
                'status': 'found' if email else 'not_found'
            })
            time.sleep(1.5)

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/export-csv', methods=['POST'])
def export_csv():
    data = request.get_json()
    results = data.get('results', [])

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['handle', 'email', 'status'])
    writer.writeheader()
    writer.writerows(results)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=yt_emails.csv'}
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
