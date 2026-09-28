import re
with open('new_js_part2.js', 'r', encoding='utf-8') as f:
    jsContent = f.read()

with open('templates/review.html', 'r', encoding='utf-8') as f:
    html = f.read()

html = re.sub(r'<script>.*?</script>', jsContent, html, flags=re.DOTALL)
with open('templates/review.html', 'w', encoding='utf-8') as f:
    f.write(html)
