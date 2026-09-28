import os, re
replacements = {
    r'\bw-64\b': 'w-60',
    r'\bw-72\b': 'w-60',
    r'\bh-16\b': 'h-14',
    r'\bh-20\b': 'h-14',
    r'\bp-6\b': 'p-4',
    r'\bp-8\b': 'p-4',
    r'\bgap-6\b': 'gap-3',
    r'\bgap-8\b': 'gap-3',
    r'\bpy-12\b': 'py-6',
    r'\bmb-8\b': 'mb-4',
}
for root, dirs, files in os.walk('templates'):
    for file in files:
        if file.endswith('.html'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            orig = content
            for pat, repl in replacements.items():
                content = re.sub(pat, repl, content)
            if orig != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f'Updated {filepath}')
