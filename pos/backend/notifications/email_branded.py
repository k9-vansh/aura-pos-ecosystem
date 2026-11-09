"""The bold, poster-style e-mails a person gets from the salon itself: a client's welcome, a team member's login and a
re-sent PIN. Loud colour on the outside, a white card with the salon's logo, a big headline, the PIN in a dashed "coupon"
pill, one button, a few tiles about what the app does and three steps to start.

Indigo, violet and sunshine yellow. Built like every other mail of the POS: tables and inline styles, no scripts, no
remote images (the logo travels inside the message), everything that came from a person or a database escaped.
"""
from notifications.email_layout import brand, esc, parse_text

OUTER, VIOLET, VIOLET_DEEP, SUN, SUN_SOFT = '#2b1d78', '#5a3fe0', '#3b27b0', '#ffd23f', '#fff1bf'
LILAC, MINT, INK, MUTED, CARD = '#ece8ff', '#d8f6ec', '#1b1440', '#6a6489', '#ffffff'
DISPLAY = "'Arial Black','Segoe UI Black',Impact,'Helvetica Neue',Arial,sans-serif"
SANS = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# event -> (small line over the headline, the headline, button label, tiles (symbol, title, line), the three steps)
PAGES = {
    'client.welcome': ("HERE'S YOUR", 'WELCOME', 'Open my app', [
        ('\U0001F45B', 'Wallet', 'Advance, cashback, cards and memberships.'),
        ('\U0001F9FE', 'Bills', 'Every bill, whenever you need it.'),
        ('\U0001F4C5', 'Visits', 'Book and manage your appointments.')],
        ['Open the app with the button above', 'Type your mobile number', 'Enter the PIN from this e-mail']),
    'staff.welcome': ('YOUR TEAM LOGIN', 'WELCOME ABOARD', 'Open the staff app', [
        ('\U0001F4BC', 'Job card', 'What you were credited, day by day.'),
        ('\U0001F4CA', 'Month', 'Your month at a glance.'),
        ('\U0001F4C5', 'Appointments', 'Who is booked with you, and when.')],
        ['Open the staff app with the button above', 'Type your mobile number', 'Enter the PIN from this e-mail']),
    'client.pin': ("HERE'S YOUR", 'PIN', 'Open my app', [
        ('\U0001F510', 'Keep it private', 'Only you and our counter should hear it.'),
        ('\U0001F6E1', 'It protects you', 'We ask before your card or advance is used.'),
        ('\U0001F4AC', 'Forgot it?', 'Open the app or ask at the counter.')],
        ['Open the app with the button above', 'Type your mobile number', 'Enter this PIN']),
}


def _first(person):
    name = (getattr(person, 'full_name', '') or getattr(person, 'first_name', '') or '').strip()
    return name.split()[0] if name else ''


def render(event, subject, text, *, user=None, client=None, url='', center=None):
    eyebrow, headline, label, tiles, steps = PAGES[event]
    blocks, link, _hint = parse_text(text)
    link = link or (url if str(url).startswith('http') else '')
    facts = {k.lower(): v for b in blocks if b['kind'] == 'facts' for k, v in b['rows']}
    paras = [b['text'] for b in blocks if b['kind'] == 'p']
    pin, phone = facts.get('your pin', ''), facts.get('mobile number', '')
    person = user or client
    hello = f'Hi {esc(_first(person))},' if _first(person) else 'Hello,'
    place = ''
    if center is not None:
        place = (getattr(center, 'display_name', '') or getattr(center, 'center_name', '') or '').strip()
    name = brand()
    intro = paras[0] if paras else ''
    note = paras[-1] if len(paras) > 1 else ''

    pin_block = ''
    if pin:
        pin_block = (
            f'<table role="presentation" align="center" cellpadding="0" cellspacing="0" style="margin:0 auto;"><tr>'
            f'<td align="center" bgcolor="{LILAC}" style="background:{LILAC};border:3px dashed {VIOLET};border-radius:999px;padding:14px 40px;">'
            f'<div style="font-family:{SANS};font-size:11px;font-weight:800;letter-spacing:.24em;text-transform:uppercase;color:{MUTED};padding-bottom:2px;">Your PIN</div>'
            f'<div style="font-family:{DISPLAY};font-size:38px;font-weight:900;letter-spacing:.3em;color:{VIOLET_DEEP};padding-left:.3em;">{esc(pin)}</div>'
            f'</td></tr></table>')
        if phone:
            pin_block += (f'<p style="margin:14px 0 0;text-align:center;font-family:{SANS};font-size:13px;color:{MUTED};">'
                          f'Your mobile number <b style="color:{INK};">{esc(phone)}</b> + this PIN = your login</p>')

    button = ''
    if link:
        button = (f'<table role="presentation" align="center" cellpadding="0" cellspacing="0" style="margin:22px auto 4px;"><tr>'
                  f'<td bgcolor="{VIOLET}" style="background:{VIOLET};border-radius:999px;"><a href="{esc(link)}" style="display:inline-block;padding:16px 44px;'
                  f'font-family:{DISPLAY};font-size:13px;font-weight:900;letter-spacing:.14em;text-transform:uppercase;color:#ffffff;'
                  f'text-decoration:none;border-radius:999px;">{esc(label)}</a></td></tr></table>'
                  f'<p style="margin:10px 0 0;text-align:center;font-family:{SANS};font-size:11px;color:{MUTED};word-break:break-all;">'
                  f'If the button does not open, copy this link: <a href="{esc(link)}" style="color:{VIOLET};">{esc(link)}</a></p>')

    tints = [(LILAC, VIOLET_DEEP), (SUN_SOFT, '#7a5b00'), (MINT, '#0d6b4f')]
    tile_cells = ''.join(
        f'<td width="33%" valign="top" style="padding:0 4px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td align="center" bgcolor="{tints[i % 3][0]}" style="background:{tints[i % 3][0]};border-radius:22px;padding:18px 8px 16px;">'
        f'<div style="font-size:30px;line-height:1.1;padding-bottom:8px;">{symbol}</div>'
        f'<div style="font-family:{DISPLAY};font-size:13px;font-weight:900;letter-spacing:.04em;text-transform:uppercase;color:{tints[i % 3][1]};padding-bottom:6px;">{esc(title)}</div>'
        f'<div style="font-family:{SANS};font-size:12px;line-height:1.45;color:{INK};">{esc(line)}</div>'
        f'</td></tr></table></td>' for i, (symbol, title, line) in enumerate(tiles))

    def heading(text_):
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:30px 0 16px;"><tr>'
                f'<td style="border-top:2px solid {INK};font-size:0;line-height:0;" width="16%">&nbsp;</td>'
                f'<td align="center" style="padding:0 12px;font-family:{DISPLAY};font-size:19px;font-weight:900;letter-spacing:.06em;text-transform:uppercase;color:{VIOLET_DEEP};white-space:nowrap;">{esc(text_)}</td>'
                f'<td style="border-top:2px solid {INK};font-size:0;line-height:0;" width="16%">&nbsp;</td></tr></table>')

    step_rows = ''.join(
        f'<tr><td width="44" valign="middle" style="padding:6px 0;"><table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td align="center" width="34" height="34" bgcolor="{SUN}" style="background:{SUN};border-radius:999px;font-family:{DISPLAY};font-size:15px;font-weight:900;color:{INK};">{i}</td></tr></table></td>'
        f'<td valign="middle" style="padding:6px 0;font-family:{SANS};font-size:14px;font-weight:600;color:{INK};">{esc(step)}</td></tr>'
        for i, step in enumerate(steps, 1))

    note_block = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:22px 0 0;"><tr>'
                  f'<td bgcolor="{SUN_SOFT}" style="background:{SUN_SOFT};border-radius:18px;padding:15px 18px;'
                  f'font-family:{SANS};font-size:13px;line-height:1.6;color:{INK};"><b>&#9888;&#65039; Keep it private.</b> {esc(note)}</td></tr></table>') if note else ''

    preheader = esc((intro or subject)[:110])
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:{OUTER};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{OUTER};">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{OUTER}" style="background:{OUTER};"><tr><td align="center" style="padding:26px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
<tr><td bgcolor="{CARD}" style="background:{CARD};border-radius:36px;padding:24px 22px 30px;">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:4px 0 20px;">
<img src="cid:geetanjali-logo" alt="{esc(name)}" width="180" style="display:block;width:180px;max-width:60%;height:auto;border:0;outline:none;font-family:{DISPLAY};font-size:20px;color:{INK};"></td></tr></table>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="{VIOLET}" align="center" style="background:{VIOLET};border-radius:28px;padding:30px 16px 34px;">
<table role="presentation" align="center" cellpadding="0" cellspacing="0"><tr><td bgcolor="{SUN}" style="background:{SUN};border-radius:999px;padding:7px 18px;font-family:{DISPLAY};font-size:11px;font-weight:900;letter-spacing:.22em;text-transform:uppercase;color:{INK};">{esc(eyebrow)}</td></tr></table>
<div style="font-family:{DISPLAY};font-size:52px;line-height:1.02;font-weight:900;letter-spacing:.01em;color:#ffffff;padding-top:16px;">{esc(headline)}</div>
<div style="font-family:{SANS};font-size:20px;letter-spacing:.5em;color:{SUN};padding-top:12px;padding-left:.5em;">&#10022; &#10022; &#10022;</div>
</td></tr></table>

<p style="margin:26px 0 8px;text-align:center;font-family:{DISPLAY};font-size:21px;font-weight:900;color:{INK};">{hello}</p>
<p style="margin:0 8px 24px;text-align:center;font-family:{SANS};font-size:15px;line-height:1.65;color:#3a3358;">{esc(intro)}</p>
{pin_block}
{button}

{heading("What is inside")}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{tile_cells}</tr></table>

{heading("Start in 3 steps")}
<table role="presentation" align="center" cellpadding="0" cellspacing="0" style="margin:0 auto;">{step_rows}</table>
{note_block}
</td></tr>

<tr><td align="center" style="padding:20px 10px 4px;font-family:{SANS};font-size:12px;line-height:1.7;color:#b9b0e6;">
<span style="font-family:{DISPLAY};font-size:15px;font-weight:900;letter-spacing:.16em;text-transform:uppercase;color:{SUN};">{esc(name)}</span><br>
{esc(place) + '<br>' if place else ''}This is an automatic message, please do not reply to it.
</td></tr></table></td></tr></table></body></html>'''

    plain = '\n\n'.join(part for part in [
        hello.replace('&amp;', '&'), intro,
        '\n'.join(x for x in (f'Mobile number: {phone}' if phone else '', f'Your PIN: {pin}' if pin else '') if x),
        f'{label}: {link}' if link else '', note, f'-- {name}'] if part)
    return {'html': html, 'text': plain}
