"""
Shared plumbing for spreadsheet imports.

Every bulk-upload endpoint had its own copy of file reading, column
normalisation and numeric parsing, and they diverged. Worse, three of them
ended in:

    except Exception as e:
        return Response({'error': str(e),
                         'detail': traceback.format_exc()}, status=400)

which does two bad things at once. It ships a full Python traceback - file
paths, code structure, sometimes SQL - to whoever called the endpoint. And
because `inventory/views.py`, `staff/views.py` and `services/views.py` contain
no logging at all, the failure was written NOWHERE on the server.

That is why the eight consecutive
`POST /staff/api/members/bulk_upload/ 400` entries in django.log carry no
diagnostic information: the reason went to the operator's browser and was lost.

`bulk_upload_guard` fixes both halves - it logs the traceback server-side and
returns a short, actionable message to the client.
"""

import logging
from contextlib import contextmanager

from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError as DRFValidationError
from rest_framework.response import Response

logger = logging.getLogger('bulk_upload')

# A spreadsheet larger than this is almost certainly a mistake, and importing it
# holds a write transaction open long enough to block the tills.
MAX_ROWS = 5000

# Recognised spreadsheet extensions.
CSV_SUFFIXES = ('.csv', '.txt', '.tsv')
EXCEL_SUFFIXES = ('.xlsx', '.xlsm', '.xls')


class BulkUploadError(Exception):
    """A problem with the uploaded file that the operator can act on.

    The message is shown to them, so it must say what to change.
    """

    def __init__(self, message, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def as_response(self):
        payload = {'error': self.message}
        payload.update(self.context)
        return Response(payload, status=status.HTTP_400_BAD_REQUEST)


@contextmanager
def bulk_upload_guard(kind, request=None):
    """Wrap a bulk-upload handler.

    * A BulkUploadError becomes a 400 with its actionable message.
    * Anything else is LOGGED with its traceback and becomes a generic 400,
      with no internals in the response body.

    Usage:
        with bulk_upload_guard('products', request) as guard:
            ...
            guard.result = Response({...})
        return guard.response
    """
    class Guard:
        result = None
        response = None

    guard = Guard()
    try:
        yield guard
        guard.response = guard.result
    except BulkUploadError as exc:
        logger.info('[Bulk upload: %s] rejected: %s', kind, exc.message)
        guard.response = exc.as_response()
    except APIException:
        # PermissionDenied, NotFound, ValidationError and friends already carry
        # the right status code. Swallowing them into a generic 400 would report
        # "bad file" for what is actually "you may not do that".
        raise
    except Exception:
        # The traceback goes to the LOG, never to the client.
        who = getattr(getattr(request, 'user', None), 'email', 'unknown')
        logger.exception('[Bulk upload: %s] failed for %s', kind, who)
        guard.response = Response(
            {'error': 'The file could not be imported. The details have been logged; '
                      'please send the file to your administrator.'},
            status=status.HTTP_400_BAD_REQUEST,
        )


def normalize_column(name):
    """Canonical form of a spreadsheet heading: lowercase, no spaces or underscores."""
    return str(name or '').strip().lower().replace(' ', '').replace('_', '').replace('-', '')


# The downloadable templates carry a grey "Required / Optional" help row and (vendors, products)
# one worked example. Uploading the template as it came used to create a vendor named
# "Required" and a vendor called "Acme Supplies"; those rows are never data.
_HELP_WORDS = {
    'required', 'optional', 'optional(forupdate)', 'optionalnumber', 'leaveblankfornew', 'retail/professional',
    'number', 'number(e.g.100.00)', 'number(e.g.10)', 'fraction(e.g.0.18)', 'true/false',
    'yyyy-mm-ddordd-mm-yyyy',
}
_EXAMPLE_SIGNATURES = (
    {'acme supplies', 'vnd-001'},
    {'loreal shampoo', 'prd-001'},
)


def is_template_row(row):
    """True for the template's help row or worked example, which are not records to import."""
    values = {str(v).strip().lower() for v in (row.values() if isinstance(row, dict) else row)
              if str(v).strip() and str(v).strip().lower() not in BLANKS}
    squashed = {v.replace(' ', '') for v in values}
    if len(squashed & _HELP_WORDS) >= 2:
        return True
    return any(signature <= values for signature in _EXAMPLE_SIGNATURES)


def read_tabular_file(file_obj, max_rows=MAX_ROWS):
    """Read an uploaded CSV/Excel file into a list of normalised-key dicts.

    Raises BulkUploadError with a message the operator can act on, rather than
    letting a pandas parse error surface as an opaque 400.
    """
    if not file_obj:
        raise BulkUploadError('No file was uploaded. Choose a .csv or .xlsx file.')

    name = (getattr(file_obj, 'name', '') or '').lower()

    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas is in requirements
        raise BulkUploadError('Spreadsheet support is not installed on the server.')

    try:
        if name.endswith(CSV_SUFFIXES):
            frame = pd.read_csv(file_obj, dtype=str, keep_default_na=False)
        elif name.endswith(EXCEL_SUFFIXES):
            frame = pd.read_excel(file_obj, dtype=str)
        else:
            raise BulkUploadError(
                'Unsupported file type.',
                supported=['.csv', '.xlsx', '.xls'],
                received=name.rsplit('.', 1)[-1] if '.' in name else 'unknown',
            )
    except BulkUploadError:
        raise
    except Exception as exc:
        # A corrupt or password-protected file, or a .xlsx that is really a .csv.
        logger.warning('[Bulk upload] could not parse %s: %s', name, exc)
        raise BulkUploadError(
            'That file could not be opened. Make sure it is a genuine .csv or .xlsx '
            'file, is not password protected, and is not still open in Excel.'
        )

    frame = frame.fillna('')
    frame.columns = [normalize_column(c) for c in frame.columns]

    rows = frame.to_dict('records')
    if not rows:
        raise BulkUploadError(
            'The file has headings but no data rows. Fill in at least one row.'
        )
    if len(rows) > max_rows:
        raise BulkUploadError(
            f'The file has {len(rows)} rows, which is over the {max_rows} row limit. '
            f'Split it into smaller files.',
            rows=len(rows), limit=max_rows,
        )
    return rows


def require_columns(rows, recognised, required_any, label='file'):
    """Check the headings before processing anything.

    A bare 400 with no explanation is what the operator used to get; this tells
    them which headings were found and which are needed.
    """
    found = {k for k in (rows[0] or {}) if k}
    matched = found & set(recognised)
    if not matched:
        raise BulkUploadError(
            f'None of the column headings in this {label} were recognised.',
            columns_found=sorted(found)[:25],
            expected=sorted(recognised)[:25],
            hint='Use the Download Template button and keep the heading row intact.',
        )
    if required_any and not (found & set(required_any)):
        raise BulkUploadError(
            f'The {label} is missing a required column.',
            columns_found=sorted(found)[:25],
            need_one_of=sorted(required_any),
        )
    return matched


BLANKS = ('', 'nan', 'none', 'null', 'na', 'n/a', '-')


def cell(row, *keys, default=''):
    """First non-blank value among `keys`, as a stripped string.

    Handles the pandas habit of turning an empty cell into the string 'nan',
    which the old code checked for inconsistently and sometimes stored.
    """
    for key in keys:
        if key not in row:
            continue
        text = str(row.get(key) if row.get(key) is not None else '').strip()
        if text.lower() not in BLANKS:
            return text
    return default


def cell_decimal(row, *keys, default=None):
    """Decimal from the first usable cell, or `default`. Never raises."""
    from pos_backend.money import D

    raw = cell(row, *keys)
    if raw == '':
        return default
    # Tolerate thousands separators and a stray currency symbol.
    cleaned = raw.replace(',', '').replace('₹', '').replace('Rs', '').strip()
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError):
        return default


def cell_int(row, *keys, default=None):
    """Integer from the first usable cell, or `default`. Never raises.

    Accepts '5', '5.0' and ' 5 ' - Excel commonly hands back floats for
    integer columns.
    """
    value = cell_decimal(row, *keys)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError, ArithmeticError):
        return default


def cell_bool(row, *keys, default=True):
    """Boolean from the first usable cell."""
    raw = cell(row, *keys).lower()
    if raw == '':
        return default
    if raw in ('true', '1', 'yes', 'y', 't', 'active', 'enabled'):
        return True
    if raw in ('false', '0', 'no', 'n', 'f', 'inactive', 'disabled'):
        return False
    return default


def cell_date(row, *keys, default=None):
    """Date from the first usable cell, trying the formats people actually type."""
    import datetime as _dt

    raw = cell(row, *keys)
    if raw == '':
        return default
    raw = raw.split(' ')[0]  # drop a time component if Excel added one
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%b-%Y', '%Y/%m/%d'):
        try:
            return _dt.datetime.strptime(raw, fmt).date()
        except (TypeError, ValueError):
            continue
    return default


def cell_percent(row, *keys, default=None):
    """Percentage from a cell, tolerating both '18' and '18%'.

    Deliberately does NOT treat a small number as a fraction. The old code did
    `if 0 < value <= 1: value *= 100`, which silently turned a genuine 1% tax
    rate into 100%.
    """
    raw = cell(row, *keys)
    if raw == '':
        return default
    value = cell_decimal({'v': raw.rstrip('%').strip()}, 'v')
    if value is None:
        return default
    if value < 0 or value > 100:
        return default
    return value


class ImportReport:
    """Accumulates per-row outcomes so the response is honest about what happened."""

    def __init__(self, max_reported=50):
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.problems = []
        self.max_reported = max_reported

    def skip(self, row_number, reason):
        self.skipped += 1
        self.problems.append(f'Row {row_number}: {reason}')

    def as_dict(self):
        payload = {
            'message': (f'Import finished: {self.created} created, '
                        f'{self.updated} updated, {self.skipped} skipped.'),
            'created': self.created,
            'updated': self.updated,
            'skipped': self.skipped,
        }
        if self.problems:
            payload['warnings'] = self.problems[:self.max_reported]
            # The old code truncated to 20 silently, so an import with 500
            # problems looked like an import with 20.
            if len(self.problems) > self.max_reported:
                payload['warnings_truncated'] = len(self.problems) - self.max_reported
                payload['total_warnings'] = len(self.problems)
        return payload


# ─── Which branches an import writes to ──────────────────────────────────────
#
# The owner's intent: upload a master list ONCE and have it appear at every
# branch, then change a price or a name at one branch without touching the
# others. For Product, Vendor and StaffMember that means one row per branch (they
# each carry a `center` FK, so the rows are independent afterwards). For services
# it means one org-level ServiceMaster plus a per-branch CenterService override.
#
# Each importer used to resolve this itself, and they disagreed:
#
#   products  scope_to_centers(...) - correct, permission-scoped
#   staff     scope_to_centers(...) - correct
#   vendors   Center.objects.all()  - EVERY branch in the business, with no
#             permission check at all, and an unknown center_id fell back to
#             every branch rather than failing closed. A manager of one salon
#             uploading a vendor list wrote rows into branches they cannot see.
#
# One resolver now, used by all of them.

#: Values the frontend sends for a true checkbox.
_TRUE = {'1', 'true', 'yes', 'on', 't'}


def wants_all_centers(request):
    """Did the caller explicitly ask to apply this to every branch?"""
    if request is None:
        return False
    raw = None
    if hasattr(request, 'data'):
        raw = request.data.get('apply_to_all_centers',
                              request.data.get('all_centers'))
    if raw is None and hasattr(request, 'query_params'):
        raw = request.query_params.get('apply_to_all_centers',
                                       request.query_params.get('all_centers'))
    return str(raw).strip().lower() in _TRUE if raw is not None else False


def resolve_target_centers(request, user, default_to_all=True):
    """The branches this import may write to.

    Returns `(centers, scope)` where `scope` is 'center' or 'all', so the
    response can tell the operator what actually happened instead of leaving them
    to guess.

    Rules:
      * `center_id` given  - that one branch, and only if the user may use it.
      * `apply_to_all_centers` - every branch the USER may write to. Never every
        branch in the business.
      * neither given - `default_to_all` decides. The response always names the
        branches, so a one-branch upload can never silently become an
        all-branches one.

    Raises BulkUploadError or PermissionDenied with a message the operator can
    act on.
    """
    from rest_framework.exceptions import PermissionDenied

    from salon_admin.models import Center
    from pos_backend.scoping import can_use_center, coerce_center_id, scope_to_centers

    raw_center = None
    if hasattr(request, 'data'):
        raw_center = request.data.get('center_id')
    if raw_center is None and hasattr(request, 'query_params'):
        raw_center = request.query_params.get('center_id')

    center_id = coerce_center_id(raw_center)

    if center_id is not None:
        if not can_use_center(user, center_id):
            raise PermissionDenied('You are not assigned to this branch.')
        center = Center.objects.filter(id=center_id).first()
        if not center:
            # Falling back to every branch here is how the vendor import wrote
            # rows the caller never asked for.
            raise BulkUploadError('That branch no longer exists.')
        return [center], 'center'

    allowed = list(scope_to_centers(Center.objects.all(), user, field='id'))
    if not allowed:
        raise PermissionDenied(
            'You are not assigned to any branch, so there is nowhere to import '
            'this.')

    if wants_all_centers(request) or default_to_all:
        return allowed, 'all'

    if len(allowed) == 1:
        return allowed, 'center'

    raise BulkUploadError(
        'Choose a branch, or tick "apply to all branches".',
        branches=[c.display_name or c.center_name for c in allowed],
        hint=('Sending neither would guess for you, and a master list imported '
              'into the wrong branch is tedious to unpick.'))


def describe_targets(centers, scope):
    """The part of an import response that says where the rows went."""
    names = [c.display_name or c.center_name for c in centers]
    payload = {'applied_to': names, 'branch_count': len(names), 'scope': scope}
    if scope == 'all' and len(names) > 1:
        payload['note'] = (
            f'Imported into all {len(names)} of your branches. Each branch keeps '
            f'its own copy, so changing a price or a name at one does not affect '
            f'the others.')
    return payload
