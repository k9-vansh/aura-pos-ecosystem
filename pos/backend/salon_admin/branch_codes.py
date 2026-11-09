"""Branch codes: the first part of every invoice number (DL01-2627-00045).

Owner decisions F20 / D12: branch code, financial year, sequence, no slashes.
A code is the branch's state (from the first two digits of its GSTIN) and a
two-digit counter within that state, in the order the branches were created.
It can be changed in Admin > Centres; it is unique.
"""

# GST state code -> short state code.
STATE_BY_GST_CODE = {
    '01': 'JK', '02': 'HP', '03': 'PB', '04': 'CH', '05': 'UK', '06': 'HR',
    '07': 'DL', '08': 'RJ', '09': 'UP', '10': 'BR', '11': 'SK', '12': 'AR',
    '13': 'NL', '14': 'MN', '15': 'MZ', '16': 'TR', '17': 'ML', '18': 'AS',
    '19': 'WB', '20': 'JH', '21': 'OD', '22': 'CG', '23': 'MP', '24': 'GJ',
    '26': 'DN', '27': 'MH', '29': 'KA', '30': 'GA', '32': 'KL', '33': 'TN',
    '34': 'PY', '36': 'TS', '37': 'AP', '38': 'LA',
}

FALLBACK_PREFIX = 'BR'


def state_prefix(gst_number):
    gst = (gst_number or '').strip()
    return STATE_BY_GST_CODE.get(gst[:2], FALLBACK_PREFIX)


def next_code(prefix, taken):
    """The first free code for `prefix` given the codes already `taken`."""
    n = 1
    while True:
        code = f'{prefix}{n:02d}' if n < 100 else f'{prefix}{n:03d}'
        if code not in taken:
            return code
        n += 1


def suggest(center, taken=None):
    """A free code for `center`. `taken` defaults to every code in use."""
    if taken is None:
        from salon_admin.models import Center
        taken = set(Center.all_objects.exclude(pk=center.pk)
                    .exclude(branch_code__isnull=True).values_list('branch_code', flat=True))
    return next_code(state_prefix(center.gst_number), taken)
