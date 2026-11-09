from rest_framework import permissions

# Map HTTP methods to the permission verb stored in Role.permissions.
# Shape (set by the Roles screen):
#   {"all_centers": bool,
#    "<module>": {"<submodule>": {"read": b, "create": b, "update": b, "delete": b}}}
METHOD_TO_VERB = {
    'GET': 'read',
    'HEAD': 'read',
    'OPTIONS': 'read',
    'POST': 'create',
    'PUT': 'update',
    'PATCH': 'update',
    'DELETE': 'delete',
}


def is_superuser(user):
    """The one built-in identity with full access (owner decision #6).

    Everyone else gets exactly what their role grants. There is no special role
    NAME any more: a role called "owner" used to receive everything, so renaming
    it broke access and creating one handed out the whole system.
    """
    return bool(user and getattr(user, 'is_authenticated', False)
                and getattr(user, 'is_superuser', False))


class IsOwner(permissions.BasePermission):
    """Superuser-only access.

    Kept under its old name because about eighty call sites use
    `IsOwner.check_is_owner` to mean "may see every branch / bypass the role";
    it now means exactly "is the superuser". Role holders who need all branches
    get the role's `all_centers` flag; role holders who need a former owner-only
    action get that action's tick (see has_action_permission).
    """
    @staticmethod
    def check_is_owner(user):
        return is_superuser(user)

    def has_permission(self, request, view):
        return self.check_is_owner(request.user)


IsSuperuser = IsOwner


class IsOwnerOrSelf(permissions.BasePermission):
    """The superuser, or the user the object belongs to (profile endpoints)."""
    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_superuser(user):
            return True
        if hasattr(obj, 'user') and obj.user == user:
            return True
        if obj == user:
            return True
        return False


def role_permissions(user):
    """Return the role permission dict for a user, or {}."""
    role = getattr(user, 'role', None)
    perms = getattr(role, 'permissions', None)
    return perms if isinstance(perms, dict) else {}


def has_module_permission(user, module, verb, submodule=None):
    """Check Role.permissions for `verb` on `module` (optionally a submodule).

    Owners and superusers always pass. With no submodule given, the check
    succeeds if ANY submodule of that module grants the verb - which matches how
    the frontend route guard decides whether to show a module at all.

    Module and submodule keys are resolved through the shared tree's legacy
    aliases, so a role saved before Marketing > Campaign was split into
    promotions/cards/memberships/packages still grants all four, and a role that
    stored `appointment` still matches a check for `appointments`.
    """
    from pos_backend.permission_tree import (
        candidate_module_keys, candidate_submodule_keys, offered_verbs,
    )

    if IsOwner.check_is_owner(user):
        return True
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    # A tick only ever grants the verbs the tree offers for it: a report tick is
    # read-only, whatever a stored role file says.
    if submodule is not None:
        offered = offered_verbs().get((module, submodule))
        if offered is not None and verb not in offered:
            return False

    permissions_dict = role_permissions(user)

    for module_key in candidate_module_keys(module):
        mod = permissions_dict.get(module_key)
        if not isinstance(mod, dict):
            continue

        if submodule is not None:
            for sub_key in candidate_submodule_keys(module, submodule):
                sub = mod.get(sub_key)
                if isinstance(sub, dict) and sub.get(verb):
                    return True
            continue

        for sub in mod.values():
            if isinstance(sub, dict) and sub.get(verb):
                return True
    return False


def has_action_permission(user, module, submodule, action):
    """True if the role grants `action` (an action tick) on module.submodule.

    Actions are the things that are not plain read/create/update/delete -
    cancelling a bill, locking payroll, amending a closing. They are listed per
    submodule in permission_tree.json and stored as
        role.permissions[module][submodule]['actions'][action] = true
    """
    from pos_backend.permission_tree import (
        candidate_module_keys, candidate_submodule_keys,
    )

    if is_superuser(user):
        return True
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    permissions_dict = role_permissions(user)
    for module_key in candidate_module_keys(module):
        mod = permissions_dict.get(module_key)
        if not isinstance(mod, dict):
            continue
        for sub_key in candidate_submodule_keys(module, submodule):
            sub = mod.get(sub_key)
            actions = sub.get('actions') if isinstance(sub, dict) else None
            if isinstance(actions, dict) and actions.get(action) is True:
                return True
    return False


def require_action(user, module, submodule, action, message=None):
    """Raise PermissionDenied unless the user holds the action tick."""
    from rest_framework.exceptions import PermissionDenied

    if not has_action_permission(user, module, submodule, action):
        raise PermissionDenied(message or (
            f'This needs the "{action.replace("_", " ")}" permission on '
            f'{module} > {submodule}. Ask an administrator to add it to your role.'))


class HasModulePermission(permissions.BasePermission):
    """Enforce Role.permissions server-side.

    Before this existed, module permissions were checked only by the Angular
    route guard reading localStorage - so any authenticated user could call
    payroll lock, stock audit, apply_promo or DELETE an invoice directly.

    Usage on a viewset or APIView:

        class PayrollRecordViewSet(viewsets.ModelViewSet):
            permission_classes = [IsAuthenticated, HasModulePermission]
            permission_module = 'staff'
            permission_submodule = 'payrolls'

    Optionally pin specific verbs per action:

        permission_action_verbs = {'lock': 'update', 'mark_paid': 'update'}
    """

    message = 'You do not have permission to perform this action.'

    def has_permission(self, request, view):
        module = getattr(view, 'permission_module', None)
        if not module:
            # Nothing declared - defer to the other permission classes rather
            # than silently allowing or silently denying.
            return True

        if IsOwner.check_is_owner(request.user):
            return True

        action = getattr(view, 'action', None)

        # ── Actions that need an action tick, not a CRUD verb ────────────
        #
        #     permission_action_ticks = {'cancel': ('invoices', 'cancel')}
        #
        # The tick is the whole requirement for that action: cancelling a
        # bill used to mean "delete on any billing tab", so anyone allowed to
        # delete a draft could also cancel a paid bill.
        ticks = getattr(view, 'permission_action_ticks', None) or {}
        if action in ticks:
            tick_sub, tick_action = ticks[action]
            if has_action_permission(request.user, module, tick_sub, tick_action):
                return True
            # The same action may also be ticked on another screen that offers
            # it (Admin > Bills offers Cancel on the very same bills).
            also = (getattr(view, 'permission_action_ticks_also', None) or {}).get(action, ())
            for other_module, other_sub, other_action in also:
                if has_action_permission(request.user, other_module, other_sub, other_action):
                    return True
            self.message = (f'This needs the "{tick_action.replace("_", " ")}" permission on '
                            f'{module} > {tick_sub}. Ask an administrator to add it to your role.')
            return False

        # ── The verb this action really is ───────────────────────────────
        #
        # Every custom @action is a POST, and POST maps to 'create'. So
        # `payrolls/lock`, `payrolls/mark_paid` and `shifts/close_shift` all
        # demanded CREATE permission when they are plainly updates: a user
        # granted Edit on Payrolls was told "You do not have permission".
        verbs = getattr(view, 'permission_action_verbs', None) or {}
        verb = verbs.get(action) or METHOD_TO_VERB.get(request.method, 'read')

        # ── The submodule this action really belongs to ──────────────────
        #
        # An action can live on a viewset that belongs to a different submodule
        # than the action does. `products/checkout` and `products/audit` hang off
        # ProductViewSet, so they demanded permission on Product Master rather
        # than on Checkout and Stock Audit - the two submodules that actually
        # govern them.
        action_submodules = getattr(view, 'permission_action_submodules', None) or {}
        submodule = action_submodules.get(
            action, getattr(view, 'permission_submodule', None))

        if has_module_permission(request.user, module, verb, submodule):
            return True

        # ── Reading is governed at module level ─────────────────────────
        #
        # A list endpoint is usually shared by several tabs of one module: the
        # product list is read by Product Master, by Checkout and by Stock
        # Audit. Requiring the exact submodule meant a user granted only
        # Checkout could not load the products they were checking out.
        #
        # So: to READ anything in a module, any submodule of that module
        # granting read is enough - which is exactly how the menu decides
        # whether to show the module. WRITES stay strictly per-submodule, so
        # "can see Inventory" never becomes "can edit the product master".
        # Reports opt out with `permission_strict_read = True`: a finance
        # report must not be readable just because the role can open some
        # other finance tab (a petty-cash clerk reading revenue reports).
        if (verb == 'read' and submodule is not None
                and not getattr(view, 'permission_strict_read', False)):
            if has_module_permission(request.user, module, 'read', None):
                return True

        # ── Shared reference data ────────────────────────────────────────
        #
        # Some endpoints are owned by one module but READ by others. The
        # customer list belongs to Admin > Clients, but the till has to look a
        # customer up to bill them; the service catalogue belongs to
        # Admin > Services, but Billing and Appointments both read it.
        #
        # Governing those endpoints by their owning submodule alone means a
        # receptionist who can bill cannot find the customer to bill - the
        # module boundaries do not match how the screens actually use the data.
        #
        # `permission_read_also` lists the other modules whose access also
        # grants READ here. Writes always require the owning submodule, so a
        # receptionist can find and create a walk-in customer but cannot edit
        # the customer master.
        if verb == 'read':
            for other_module, other_sub in getattr(view, 'permission_read_also', ()) or ():
                if has_module_permission(request.user, other_module, 'read', other_sub):
                    return True

        # A creating verb may also be delegated - billing must be able to
        # register a walk-in customer mid-sale.
        if verb == 'create':
            for other_module, other_sub in getattr(view, 'permission_create_also', ()) or ():
                if has_module_permission(request.user, other_module, 'create', other_sub):
                    return True

        return False


def module_permission(module, submodule=None, **attrs):
    """Put a function view (@api_view) under HasModulePermission.

        @module_permission('dashboard', 'summary',
                           permission_read_also=[('overview', 'home')])
        @api_view(['GET'])
        def dashboard_summary(request): ...

    Function views had only IsAuthenticated (finding R2): any signed-in user,
    even one whose role grants nothing, could read every dashboard and report.
    """
    def wrap(view_func):
        cls = view_func.cls
        cls.permission_classes = [permissions.IsAuthenticated, HasModulePermission]
        cls.permission_module = module
        cls.permission_submodule = submodule
        for key, value in attrs.items():
            setattr(cls, key, value)
        return view_func
    return wrap


class ReadOnlyOrModulePermission(HasModulePermission):
    """Same as HasModulePermission but never allows writes for anyone but owners.

    For records that must not be mutated through the API at all (financial
    audit trails). Reads still respect the role's `read` flag.
    """

    def has_permission(self, request, view):
        if request.method not in permissions.SAFE_METHODS:
            return IsOwner.check_is_owner(request.user)
        return super().has_permission(request, view)
