import re
import io

def svg(path, size=16, stroke=2, class_name=""):
    cls = f' class="{class_name}"' if class_name else ''
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round"{cls}><path d="{path}"/></svg>'

paths = {
    "dollar": "M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6",
    "bar-chart": "M18 20V10M12 20V4M6 20v-4",
    "pin": "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z M12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "scissors": "M20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12",
    "message": "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    "receipt": "M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1-2-1Z M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8 M12 17V7",
    "calendar": "M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
    "credit-card": "M21 4H3a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h18a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM1 10h22",
    "briefcase": "M16 20V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16M2 14h20M2 14v4a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-4M2 14v-4a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v4",
    "file-text": "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
    "arrow-right": "M5 12h14M12 5l7 7-7 7",
    "zap": "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
    "settings": "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "smartphone": "M5 2h14a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z M12 18h.01",
    "droplet": "M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z",
    "trending-up": "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
    "pen-tool": "M12 19l7-7 3 3-7 7-3-3z M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z M2 2l7.586 7.586 M11 11l-4 4",
    "package": "M16.5 9.4l-9-5.19M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16zM3.27 6.96L12 12.01l8.73-5.05M12 22.08V12",
    "hash": "M4 9h16 M4 15h16 M10 3L8 21 M16 3l-2 18",
    "circle": "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z",
    "clipboard": "M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2 M15 2H9a1 1 0 0 0-1 1v2a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1z",
    "search": "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.35-4.35",
    "wallet": "M21 12V7H5a2 2 0 0 1 0-4h14v2 M3 5v14a2 2 0 0 0 2 2h16v-5 M16 13h.01",
    "alert-triangle": "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z M12 9v4 M12 17h.01",
    "building": "M4 2v20 M20 2v20 M4 10h16 M4 14h16 M4 6h16 M4 18h16",
    "ticket": "M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v2z",
    "gift": "M20 12v10H4V12 M2 7h20v5H2z M12 22V7 M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z",
    "shopping-bag": "M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z M3 6h18 M16 10a4 4 0 0 1-8 0",
    "tag": "M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z M7 7h.01",
    "star": "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z",
    "target": "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M12 12h.01",
    "award": "M12 15c2.761 0 5-2.239 5-5s-2.239-5-5-5-5 2.239-5 5 2.239 5 5 5z M8.21 13.89L7 23l5-3 5 3-1.21-9.11",
    "globe": "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M2 12h20 M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z",
    "flag": "M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z M4 22v-7"
}

mapping = {
    '💵': "dollar", '💰': "dollar", '📊': "bar-chart", '📈': "trending-up", '📉': "trending-up",
    '📍': "pin", '✂️': "scissors", '✂': "scissors", '💬': "message", '🧾': "receipt",
    '📅': "calendar", '💳': "credit-card", '💼': "briefcase", '📜': "file-text", '📝': "file-text",
    '📋': "clipboard", '➔': "arrow-right", '⚡': "zap", '⚙️': "settings", '⚙': "settings",
    '📱': "smartphone", '🧴': "droplet", '💄': "pen-tool", '📦': "package", '🔢': "hash",
    '🟣': "circle", '🔍': "search", '👛': "wallet", '⚠️': "alert-triangle", '🏦': "building",
    '🎫': "ticket", '🎁': "gift", '🛍️': "shopping-bag", '🛍': "shopping-bag", '🏷️': "tag",
    '🏷': "tag", '✨': "star", '🎯': "target", '🏆': "award", '🌐': "globe", '🇮🇳': "flag"
}

def replace_emojis(match):
    char = match.group(0)
    if char in mapping:
        path_name = mapping[char]
        p = paths[path_name]
        return svg(p)
    return char

for f_name in ['finance.html', 'finance.ts']:
    f_path = 'c:/Users/DELL/Desktop/malai-chaap/frontend/src/app/finance/' + f_name
    with io.open(f_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # We create a regex from all keys in mapping
    regex = re.compile('|'.join(re.escape(k) for k in mapping.keys()))
    new_content = regex.sub(replace_emojis, content)
    
    with io.open(f_path, 'w', encoding='utf-8') as f:
        f.write(new_content)

print('Done replacing emojis.')
