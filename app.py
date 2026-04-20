import re
import time
import requests
import io
import csv
import os
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


def clean_handle(handle):
    handle = handle.strip()
    if handle and not handle.startswith('@'):
        handle = '@' + handle
    return handle


def filter_emails(emails):
    return [
        e for e in emails
        if not any(bad in e.lower() for bad in BLOCKED_DOMAINS)
    ]


def scrape_email(handle):
    url = f'https://www.youtube.com/{handle}/about'
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        # 1. Try description simpleText fields
        desc_matches = re.findall(r'"description":\{"simpleText":"(.*?)"', resp.text)
        desc_text = ' '.join(desc_matches)
        emails = filter_emails(EMAIL_PATTERN.findall(desc_text))
        if emails:
            return emails[0]

        # 2. Try businessEmail field
        biz = re.search(
            r'businessEmail[\":\s]+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
            resp.text
        )
        if biz:
            return biz.group(1)

        # 3. Full-page raw scan
        emails = filter_emails(EMAIL_PATTERN.findall(resp.text))
        return emails[0] if emails else None

    except Exception:
        return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/find-emails', methods=['POST'])
def find_emails():
    try:
        data = request.get_json(force=True, silent=True) or {}
        raw_handles = data.get('handles', '')
        handles = [clean_handle(h) for h in raw_handles.splitlines() if h.strip()]
        handles = list(dict.fromkeys(handles))

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
            time.sleep(1.2)

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/export-csv', methods=['POST'])
def export_csv():
    data = request.get_json(force=True, silent=True) or {}
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
