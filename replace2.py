import re
with open('templates/review.html', 'r', encoding='utf-8') as f:
    c = f.read()

c = re.sub(r'font-size:\s*18px', 'font-size: 15px', c)
c = re.sub(r'padding:\s*16px 24px', 'padding: 12px 16px', c)
c = re.sub(r'padding:\s*10px 20px', 'padding: 8px 16px', c)
c = re.sub(r'padding:\s*32px', 'padding: 16px', c)

with open('templates/review.html', 'w', encoding='utf-8') as f:
    f.write(c)
