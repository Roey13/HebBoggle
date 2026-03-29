"""
server.py — שרת מרוץ מילים
משרת קבצים סטטיים + proxy לפירוש מילים ממילוג
הרצה: py server.py
גישה: http://localhost:8080/boggle-hebrew.html
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.request, urllib.parse, re, json, sys, os

PORT = int(os.environ.get('PORT', 8080))
MILOG_URL = "https://milog.co.il/{}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9"
}

# Cache תוצאות validation — נשמר כל זמן שהשרת רץ
_validate_cache = {}   # word → True/False

NIKUD = re.compile(r'[\u05b0-\u05c7\ufb1d-\ufb4e]')

FINAL_TO_NF = {'ך':'כ', 'ם':'מ', 'ן':'נ', 'ף':'פ', 'ץ':'צ'}

def normalize_final(word):
    """מחליף אות סופית בסוף מילה באות רגילה, לצורך השוואה."""
    if word and word[-1] in FINAL_TO_NF:
        return word[:-1] + FINAL_TO_NF[word[-1]]
    return word

def strip_tags(html):
    """מסיר תגיות HTML ומחזיר טקסט נקי"""
    return re.sub(r'<[^>]+>', '', html).replace('&amp;', '&').replace('&#8315;', '–').replace('&#8209;', '‑').strip()

def strip_nikud(text):
    """מסיר ניקוד מטקסט עברי"""
    return NIKUD.sub('', text)

def validate_word_in_milog(html, word):
    """
    בודק האם המילה קיימת במילוג.
    מסיר ניקוד מכל ה-HTML תחילה, ואז מחפש התאמה מדויקת
    בכותרות הערכים (class sr_e_t) — ללא שמות פרטיים.
    """
    # הסר ניקוד מכל ה-HTML לפני כל השוואה
    html_clean = strip_nikud(html)

    # מצא כל כותרת ערך: class='sr_e_t...' ... > TITLE <
    titles = re.findall(r"class='sr_e_t[^']*'[^>]*>([^<]+)", html_clean)
    for title_raw in titles:
        title_raw = title_raw.strip()
        # המילה היא החלק הראשון לפני רווח / מקף / ניקוד
        title_word = re.split(r'[\s\-–(]', title_raw)[0].strip()
        if title_word == word:
            # ודא שהערך הזה לא שם פרטי — חפש "שם פרטי" ליד הכותרת
            idx = html_clean.find(title_raw)
            ctx = html_clean[max(0, idx-20):idx+200]
            if 'שם פרטי' in ctx:
                continue
            return True
    return False

def extract_milog_html(html, word):
    """
    מחלץ את בלוקי תוצאות החיפוש (sr_e) מ-HTML של מילוג.
    מסמן את הבלוק שכותרתו תואמת את המילה המבוקשת עם class sr_e_match.
    """
    blocks = re.findall(r"<div class='sr_e'>.*?</div></div></div>", html, re.DOTALL)
    if not blocks:
        return ''

    html_clean = strip_nikud(html)
    marked = []
    for block in blocks:
        block_clean = strip_nikud(block)
        # דלג על הצעות "האם התכוונת ל"
        if 'האם התכוונת' in block_clean:
            continue
        # בדוק אם כותרת הבלוק תואמת את המילה
        title_m = re.search(r"class='sr_e_t[^']*'[^>]*>([^<]+)", block_clean)
        is_match = False
        if title_m:
            title_word = re.split(r'[\s\-–(]', title_m.group(1).strip())[0].strip()
            is_match = (normalize_final(title_word) == normalize_final(word))

        if is_match:
            block = block.replace("<div class='sr_e'>", "<div class='sr_e sr_e_match'>", 1)
        marked.append(block)

    combined = ''.join(marked)
    # נקה script / style / iframe
    combined = re.sub(r'<script[^>]*>.*?</script>', '', combined, flags=re.DOTALL)
    combined = re.sub(r'<style[^>]*>.*?</style>',  '', combined, flags=re.DOTALL)
    combined = re.sub(r'<iframe[^>]*>.*?</iframe>', '', combined, flags=re.DOTALL)
    # הסר href כדי שקישורים לא יוציאו את המשתמש מהעמוד
    combined = re.sub(r'\s*href=["\'][^"\']*["\']', '', combined)
    return combined.strip()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # שתוק בלוגים

    def do_GET(self):
        # API endpoint: /api/validate?word=...  (בדיקת קיום בלבד)
        if self.path.startswith('/api/validate'):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            word   = params.get('word', [''])[0]
            if not word:
                self._json(400, {"error": "missing word"})
                return

            # בדוק cache תחילה
            if word in _validate_cache:
                self._json(200, {"word": word, "valid": _validate_cache[word], "cached": True})
                return

            try:
                url = MILOG_URL.format(urllib.parse.quote(word))
                req = urllib.request.Request(url, headers=HEADERS)
                html = None
                # נסה עד 2 פעמים במקרה של 502
                for attempt in range(2):
                    try:
                        with urllib.request.urlopen(req, timeout=10) as r:
                            html = r.read().decode('utf-8')
                        break  # הצליח
                    except Exception as inner_e:
                        if attempt == 0:
                            import time; time.sleep(1)  # המתן שנייה ונסה שוב
                        else:
                            raise inner_e

                valid = validate_word_in_milog(html, word)
                _validate_cache[word] = valid  # שמור ב-cache
                self._json(200, {"word": word, "valid": valid})
            except Exception as e:
                # במקרה של שגיאת רשת — נניח שהמילה תקינה (לא נסיר)
                self._json(200, {"word": word, "valid": True, "error": str(e)})
            return

        # API endpoint: /api/milog?word=...
        if self.path.startswith('/api/milog'):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            word   = params.get('word', [''])[0]

            if not word:
                self._json(400, {"error": "missing word"})
                return

            try:
                url = MILOG_URL.format(urllib.parse.quote(word))
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=8) as r:
                    html = r.read().decode('utf-8')

                raw_html = extract_milog_html(html, word)
                self._json(200, {"word": word, "html": raw_html})
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            super().do_GET()

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print(f"מרוץ מילים — שרת עולה על פורט {PORT}")
    print(f"פתח בדפדפן: http://localhost:{PORT}/boggle-hebrew.html")
    print("Ctrl+C לעצירה\n")
    HTTPServer(('', PORT), Handler).serve_forever()
