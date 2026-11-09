import re
with open('finance.html', 'r', encoding='utf-8') as f:
    text = f.read()

# Remove the span badge
text = re.sub(r'<span class="card-icon-badge[^>]*>.*?</span>\s*', '', text, flags=re.DOTALL)
text = re.sub(r'<td>\s*<svg[^>]*>.*?</svg>\s*', '<td>', text, flags=re.DOTALL)

with open('finance.html', 'w', encoding='utf-8') as f:
    f.write(text)
