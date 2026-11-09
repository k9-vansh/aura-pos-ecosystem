"""The one look every e-mail the POS sends shares: a clean card with the brand, a clear headline, the facts
or items that matter, one button, and a footer that says why the mail arrived.

    render(event, subject, text, user=..., url=..., data=..., center=...) -> {'html': ..., 'text': ...}

`text` is the plain message the code already writes (the bell and WhatsApp use it too). It is read for its
paragraphs, "Label: value" lines and the first link (which becomes the button). For stock requests and
transfers the items are looked up from `data` and drawn as a table. Everything that came from a person or a
database is escaped, and nothing is loaded from outside: no remote images, no scripts.
"""
import html as _html
import re
from decimal import Decimal

from django.conf import settings

INK, MUTED, LINE, PAPER, PAGE = '#0f172a', '#64748b', '#e2e8f0', '#ffffff', '#eef2f6'
ACCENT = '#2fb5aa'
TONES = {            # tone -> (strip colour, soft background, text colour)
    'action': ('#2fb5aa', '#e8f8f6', '#0f766e'),
    'info': ('#475569', '#f1f5f9', '#334155'),
    'good': ('#16a34a', '#e8f7ee', '#166534'),
    'warn': ('#d97706', '#fef3e2', '#92400e'),
    'alert': ('#dc2626', '#fdecec', '#991b1b'),
}
# event -> (small heading above the title, tone, why you got this)
LOOK = {
    'approval.requested': ('Approval needed', 'action', 'You are an approver for this branch.'),
    'approval.decided': ('Decision', 'info', 'You asked for this.'),
    'stock.request_raised': ('Stock request', 'action', 'You can approve stock requests for this branch.'),
    'stock.request_approved': ('Ready to send', 'good', 'You send approved stock requests.'),
    'stock.transfer_sent': ('Stock on its way', 'action', 'This stock is coming to your branch.'),
    'stock.transfer_received': ('Stock received', 'good', 'You asked for, approved or sent this stock.'),
    'sap.posting_failed': ('SAP needs attention', 'alert', 'You look after the SAP connection.'),
    'closing.variance': ('Closing out of balance', 'warn', 'You review day-end closings.'),
    'payroll.ready': ('Payroll ready', 'info', 'You lock payroll.'),
    'inventory.low_stock': ('Low stock', 'warn', 'You watch stock levels for this branch.'),
    'client.welcome': ('Welcome', 'action', 'You are receiving this because you visited the salon.'),
    'staff.welcome': ('Your team login', 'action', 'You are receiving this because you are part of the salon team.'),
    'client.pin': ('Your PIN', 'action', ''),
    'client.expiry_reminder': ('A reminder', 'warn', ''),
    'client.appointment_reminder': ('Your appointment', 'action', ''),
    'client.campaign': ('', 'action', ''),
    'test': ('Test message', 'info', 'Sent from Admin > Integrations to check the mail set-up.'),
}
URL_RE = re.compile(r'https?://[^\s<>"\']+')
FACT_RE = re.compile(r'^([A-Z][A-Za-z0-9 /&()\'.-]{1,30}):\s+(\S.*)$')


def esc(value):
    return _html.escape(str(value if value is not None else ''), quote=True)


def brand():
    return getattr(settings, 'BRAND_NAME', None) or 'Aura Salon & Spa'


def base_url():
    return getattr(settings, 'APP_BASE_URL', 'http://localhost:4200').rstrip('/')


def inr(value):
    """12345.6 -> '12,345.60' with Indian digit grouping."""
    try:
        number = Decimal(str(value or 0)).quantize(Decimal('0.01'))
    except Exception:                                                     # noqa: BLE001
        return str(value)
    sign, digits = ('-' if number < 0 else ''), f'{abs(number):.2f}'
    whole, frac = digits.split('.')
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ','.join(parts + [tail])
    return f'{sign}{whole}.{frac}'


def qty(value):
    text = f'{Decimal(str(value or 0)).normalize():f}'
    return text


# ─── What to say ────────────────────────────────────────────────────────────

def parse_text(text):
    """(blocks, link, link_hint) from a plain message."""
    blocks, link, hint = [], '', ''
    paragraph, facts = [], []

    def flush():
        nonlocal paragraph, facts
        if paragraph:
            blocks.append({'kind': 'p', 'text': ' '.join(paragraph)})
            paragraph = []
        if facts:
            blocks.append({'kind': 'facts', 'rows': facts})
            facts = []

    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        found = URL_RE.search(line)
        if found and not link:
            link = found.group(0).rstrip('.,;)')
            before = (line[:found.start()] + line[found.end():]).strip().rstrip(':').strip()
            hint = before
            continue
        fact = FACT_RE.match(line)
        if fact and 'http' not in line:
            if paragraph:
                flush()
            facts.append((fact.group(1), fact.group(2)))
            continue
        if line[:2] in ('- ', '* ', '• '):
            flush()
            blocks.append({'kind': 'bullet', 'text': line[2:].strip()})
            continue
        if facts:
            flush()
        paragraph.append(line)
    flush()
    return blocks, link, hint


def _stock_request_blocks(request_id):
    from inventory.models import StockRequest
    from inventory.serializers import line_value

    req = (StockRequest.objects.filter(pk=request_id)
           .select_related('requesting_center', 'created_by', 'decided_by').first())
    if req is None:
        return []
    rows, base, gst, total = [], Decimal(0), Decimal(0), Decimal(0)
    for item in req.items.select_related('product'):
        b, g, t = line_value(item.quantity, item.product)
        base, gst, total = base + b, gst + g, total + t
        rows.append([item.product.product_code or '', item.product.name, qty(item.quantity),
                     inr(item.product.price), inr(t)])
    centre = req.requesting_center.display_name or req.requesting_center.center_name
    facts = [('Request', f'REQ-{req.pk}'), ('Branch', centre), ('Category', req.stock_type or '-'),
             ('Asked by', (req.created_by.full_name or req.created_by.email) if req.created_by_id else '-')]
    if req.decided_by_id:
        facts.append(('Decided by', req.decided_by.full_name or req.decided_by.email))
    if req.notes:
        facts.append(('Note', req.notes))
    return [{'kind': 'facts', 'rows': facts},
            {'kind': 'table', 'head': ['Code', 'Product', 'Qty', 'Rate', 'Amount'], 'right': [2, 3, 4],
             'rows': rows, 'foot': [('Total (with GST)', inr(total))]}]


def _transfer_blocks(transfer_id):
    from inventory.models import StockTransfer
    from inventory.serializers import line_value

    t = (StockTransfer.objects.filter(pk=transfer_id)
         .select_related('from_center', 'to_center', 'created_by', 'dispatched_by', 'received_by').first())
    if t is None:
        return []
    done = t.status == 'Completed'
    rows, total = [], Decimal(0)
    for item in t.items.select_related('product'):
        got = item.quantity if item.received_quantity is None else item.received_quantity
        _, _, worth = line_value(got if done else item.quantity, item.product)
        total += worth
        row = [item.product.product_code or '', item.product.name, qty(item.quantity)]
        if done:
            row.append(qty(got))
        row.append(inr(worth))
        rows.append(row)
    name = lambda c: c.display_name or c.center_name  # noqa: E731
    facts = [('Transfer', f'ST-{t.pk}'), ('From', name(t.from_center)), ('To', name(t.to_center))]
    if t.dispatched_by_id:
        facts.append(('Sent by', t.dispatched_by.full_name or t.dispatched_by.email))
    if done and t.received_by_id:
        facts.append(('Received by', t.received_by.full_name or t.received_by.email))
    if done:
        facts.append(('Bill', 'Attached' if t.bill_file else 'Not attached'))
    head = ['Code', 'Product', 'Sent'] + (['Accepted'] if done else []) + ['Amount']
    blocks = []
    if done and t.has_difference:
        blocks.append({'kind': 'note', 'tone': 'alert',
                       'text': 'Less arrived than was sent. ' + (t.received_note or '')})
    blocks += [{'kind': 'facts', 'rows': facts},
               {'kind': 'table', 'head': head, 'right': list(range(2, len(head))), 'rows': rows,
                'foot': [('Total (with GST)', inr(total))]}]
    return blocks


def _enriched(event, data):
    data = data or {}
    try:
        if event.startswith('stock.request') or (event == 'approval.decided' and data.get('stock_request_id')):
            return _stock_request_blocks(data.get('stock_request_id')) if data.get('stock_request_id') else []
        if event.startswith('stock.transfer') and data.get('stock_transfer_id'):
            return _transfer_blocks(data['stock_transfer_id'])
    except Exception:                                                   # noqa: BLE001 - a mail is never lost to a table
        return []
    return []


# ─── Drawing it ─────────────────────────────────────────────────────────────

def _first_name(person):
    name = (getattr(person, 'full_name', '') or getattr(person, 'first_name', '') or '').strip()
    return name.split()[0] if name else ''


def render(event, subject, text, *, user=None, client=None, url='', data=None, center=None):
    from notifications import email_branded
    if event in email_branded.PAGES:              # the salon's own poster-style mails (welcome, team login, PIN)
        return email_branded.render(event, subject, text, user=user, client=client, url=url, center=center)
    eyebrow, tone, why = LOOK.get(event, ('', 'info', ''))
    strip, soft, deep = TONES[tone]
    blocks, link, hint = parse_text(text)
    extra = _enriched(event, data)
    if extra:
        lead = [dict(b) for b in blocks if b['kind'] == 'p'][:1]      # the headline stays; the table says the rest
        for b in lead:
            b['text'] = b['text'].split(' Less arrived than was sent')[0]      # the red note below says it
        blocks = lead + extra
    if not link and url:
        link = url if url.startswith('http') else base_url() + (url if url.startswith('/') else '/' + url)
    person = user or client
    greeting = f'Hi {esc(_first_name(person))},' if _first_name(person) else 'Hello,'
    place = ''
    if center is not None:
        place = (getattr(center, 'display_name', '') or getattr(center, 'center_name', '') or '').strip()
    label = {'client.welcome': 'Open my app', 'staff.welcome': 'Open the staff app',
             'approval.requested': 'Review and decide', 'stock.request_raised': 'Review and decide',
             'stock.request_approved': 'Open the request', 'stock.transfer_sent': 'Open the transfer',
             'stock.transfer_received': 'Open the transfer'}.get(event, 'Open in POS')
    name = brand()

    body_html, body_text = [], []
    for block in blocks:
        kind = block['kind']
        if kind == 'p':
            body_html.append(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK};">{esc(block["text"])}</p>')
            body_text.append(block['text'])
        elif kind == 'bullet':
            body_html.append(f'<p style="margin:0 0 6px;font-size:15px;line-height:1.5;color:{INK};padding-left:14px;">&bull;&nbsp;{esc(block["text"])}</p>')
            body_text.append('- ' + block['text'])
        elif kind == 'note':
            _, nsoft, ndeep = TONES[block.get('tone', 'warn')]
            body_html.append(f'<div style="margin:0 0 14px;padding:12px 14px;background:{nsoft};border-radius:8px;font-size:14px;line-height:1.5;color:{ndeep};">{esc(block["text"])}</div>')
            body_text.append(block['text'])
        elif kind == 'facts':
            rows = ''.join(
                f'<tr><td width="130" style="width:130px;padding:7px 12px 7px 0;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:{MUTED};white-space:nowrap;vertical-align:top;">{esc(k)}</td>'
                f'<td style="padding:7px 0;font-size:14px;color:{INK};">{esc(v)}</td></tr>' for k, v in block['rows'])
            body_html.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;border-top:1px solid {LINE};border-bottom:1px solid {LINE};">{rows}</table>')
            body_text.extend(f'{k}: {v}' for k, v in block['rows'])
        elif kind == 'table':
            right = set(block.get('right', []))
            head = ''.join(
                f'<th align="{"right" if i in right else "left"}" style="padding:8px 8px;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:{MUTED};background:{PAGE};border-bottom:1px solid {LINE};">{esc(h)}</th>'
                for i, h in enumerate(block['head']))
            lines = ''.join(
                '<tr>' + ''.join(
                    f'<td align="{"right" if i in right else "left"}" style="padding:9px 8px;font-size:13px;color:{INK};border-bottom:1px solid {LINE};">{esc(c)}</td>'
                    for i, c in enumerate(row)) + '</tr>' for row in block['rows'])
            span = len(block['head'])
            foot = ''.join(
                f'<tr><td colspan="{span - 1}" align="right" style="padding:10px 8px;font-size:13px;color:{MUTED};">{esc(k)}</td>'
                f'<td align="right" style="padding:10px 8px;font-size:15px;font-weight:700;color:{INK};">&#8377; {esc(v)}</td></tr>'
                for k, v in block.get('foot', []))
            body_html.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;border:1px solid {LINE};border-radius:8px;border-collapse:separate;overflow:hidden;"><thead><tr>{head}</tr></thead><tbody>{lines}{foot}</tbody></table>')
            body_text.append('\n'.join(' | '.join(str(c) for c in row) for row in [block['head']] + block['rows']))
            body_text.extend(f'{k}: Rs {v}' for k, v in block.get('foot', []))

    button = ''
    if link:
        button = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:16px 0 12px;"><tr>'
                  f'<td bgcolor="{strip}" style="border-radius:8px;"><a href="{esc(link)}" style="display:inline-block;padding:13px 26px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:8px;">{esc(label)}</a></td></tr></table>')
        if hint:
            button += f'<p style="margin:0 0 6px;font-size:12px;color:{MUTED};">{esc(hint)}</p>'
        button += (f'<p style="margin:0 0 4px;font-size:12px;color:{MUTED};word-break:break-all;">If the button does not open, copy this link: '
                   f'<a href="{esc(link)}" style="color:{deep};">{esc(link)}</a></p>')
        body_text.append(f'{label}: {link}' + (f'\n({hint})' if hint else ''))

    why_line = why + (f' Branch: {place}.' if place and why else '')
    header_title = esc(subject)
    preheader = esc((blocks[0]['text'] if blocks and blocks[0]['kind'] == 'p' else subject)[:110])
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><title>{header_title}</title></head>
<body style="margin:0;padding:0;background:{PAGE};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{PAGE};">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{PAGE}" style="background:{PAGE};"><tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
<tr><td style="padding:0 4px 16px;font-family:Georgia,'Times New Roman',serif;font-size:20px;letter-spacing:.18em;text-transform:uppercase;color:{INK};"><img src="cid:geetanjali-logo" alt="{esc(name)}" width="170" style="display:block;width:170px;max-width:60%;height:auto;border:0;outline:none;font-size:18px;color:{INK};"></td></tr>
<tr><td bgcolor="{PAPER}" style="background:{PAPER};border-radius:12px;border-top:4px solid {strip};font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:26px 28px 8px;">
{f'<div style="display:inline-block;padding:4px 10px;margin:0 0 12px;background:{soft};color:{deep};border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;">{esc(eyebrow)}</div>' if eyebrow else ''}
<h1 style="margin:0 0 14px;font-size:22px;line-height:1.3;color:{INK};font-weight:700;">{header_title}</h1>
<p style="margin:0 0 14px;font-size:15px;color:{INK};">{greeting}</p>
{''.join(body_html)}
{button}
</td></tr></table></td></tr>
<tr><td style="padding:16px 8px 0;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:{MUTED};">
{esc(why_line) + '<br>' if why_line else ''}Sent by {esc(name)} POS. This is an automatic message; please do not reply to it.
</td></tr></table></td></tr></table></body></html>'''
    plain = '\n\n'.join(part for part in [greeting.replace('&amp;', '&'), *body_text] if part)
    plain += f'\n\n--\n{why_line + chr(10) if why_line else ""}Sent by {name} POS. This is an automatic message.'
    return {'html': html, 'text': plain}
