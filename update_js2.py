import re
with open(r'c:\Users\NAVEEN\.gemini\antigravity-ide\brain\16b1f41d-686a-4367-a3e1-6b6a8ceb7f5a\scratch\new_js.txt', 'r', encoding='utf-8') as f:
    jsContent = f.read()

with open('templates/review.html', 'r', encoding='utf-8') as f:
    html = f.read()

html = re.sub(r'<script>.*?</script>', jsContent, html, flags=re.DOTALL)
with open('templates/review.html', 'w', encoding='utf-8') as f:
    f.write(html)
