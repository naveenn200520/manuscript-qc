import re
with open('templates/review.html', 'r', encoding='utf-8') as f:
    html = f.read()

m = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
if m:
    with open('script_backup.js', 'w', encoding='utf-8') as f:
        f.write(m.group(1))
