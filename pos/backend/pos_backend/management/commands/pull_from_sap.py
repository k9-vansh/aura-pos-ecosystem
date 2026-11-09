from django.core.management.base import BaseCommand, CommandError
from pos_backend.sap_utils import _send_sap_request, _sap, SAP_URL_BASE
from clients.models import Client
from inventory.models import Product, Vendor
import logging
import re

logger = logging.getLogger(__name__)

MARKETING_ITEM = re.compile(r'^(VC|MB|PK)\d{6}$')

PAGE_SIZE = 100  # SAP B1 default is 20; we request 100 per page for efficiency


def item_attributes(item, groups):
    """What SAP says about an item, in the POS's words.

    category      the item group, title-cased (RETAIL -> Retail, CONSUMABLE-R -> Consumable-R)
    brand         the U_BRAND field
    sub_category  the U_SUB_GR field
    gst           the U_TAXRT field (18 or 5); 0 when SAP has none
    """
    from decimal import Decimal, InvalidOperation

    group = groups.get(item.get('ItemsGroupCode')) or ''
    try:
        gst = Decimal(str(item.get('U_TAXRT')).strip())
        if gst < 0 or gst > 100:
            gst = Decimal('0')
    except (InvalidOperation, ValueError):
        gst = Decimal('0')
    return {
        'category': group.strip().title(),
        'brand': (item.get('U_BRAND') or '').strip(),
        'sub_category': (item.get('U_SUB_GR') or '').strip(),
        'gst': gst,
    }


class Command(BaseCommand):
    help = 'Pull updated Business Partners and Items from SAP into the POS (two-way sync)'

    def handle(self, *args, **options):
        self.stdout.write("Starting two-way sync from SAP...")

        self._sync_business_partners()
        self._sync_items()
        self._sync_boms()

        self.stdout.write(self.style.SUCCESS("SAP two-way sync completed."))

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _paginate(self, base_url, order_by):
        """
        Generator that yields SAP B1 OData result pages using $skip.
        SAP B1 SL may force a max page size (e.g. 20) regardless of $top.
        We increment $skip by the actual number of rows returned to ensure no items are missed.

        Rows are read in a fixed order (`order_by`): $skip over an unordered list
        can repeat or miss rows when SAP changes between pages. A page that cannot
        be read stops the whole pull with an error - it used to log and carry on,
        so a half-read list finished as "SAP two-way sync completed".
        """
        skip = 0
        sep = '&' if '?' in base_url else '?'
        while True:
            url = f"{base_url}{sep}$orderby={order_by}&$skip={skip}"
            try:
                resp = _send_sap_request(
                    'GET', url, timeout=30,
                    headers={'Prefer': f'odata.maxpagesize={PAGE_SIZE}'})
            except Exception as e:
                logger.error(f"SAP pagination error at skip={skip}: {e}")
                raise CommandError(f"Reading SAP stopped at row {skip}: {e}")

            if resp.status_code != 200:
                raise CommandError(f"SAP returned HTTP {resp.status_code} for {url}")

            data = resp.json()
            rows = data.get('value', [])
            if not rows:
                break
            
            yield from rows
            
            if len(rows) == 0:
                break
                
            skip += len(rows)

    # ─── Business Partners ────────────────────────────────────────────────────

    def _sync_business_partners(self):
        self.stdout.write("Pulling Business Partners from SAP...")
        updated_clients = 0
        updated_vendors = 0

        import datetime
        from pos_backend.sap_utils import _resolve_bpl_id
        from salon_admin.models import Center
        
        centers = Center.objects.filter(is_active=True)
        center_bpl_map = {}
        for c in centers:
            bpl = _resolve_bpl_id(c.sap_branch_name, default=None)
            if bpl is not None:
                center_bpl_map[c.id] = bpl

        today_dt = datetime.date.today().strftime('%Y-%m-%dT00:00:00')
        
        # Skip Clients for now to prioritize vendors
        pass

        # Pull Vendors (with BPBranchAssignment)
        base_url_vendors = (
            f"{SAP_URL_BASE}/BusinessPartners"
            f"?$filter=CardType eq 'cSupplier'"
            f"&$select=CardCode,CardName,Phone1,EmailAddress,CardType,BPBranchAssignment"
        )

        for bp in self._paginate(base_url_vendors, 'CardCode'):
            card_code = bp.get('CardCode', '')
            card_name = bp.get('CardName', '')
            phone = bp.get('Phone1', '')
            email = bp.get('EmailAddress', '')
            branch_assignments = bp.get('BPBranchAssignment') or []
            
            assigned_bplids = set()
            for assignment in branch_assignments:
                if assignment.get('DisabledForBP') != 'tYES' and assignment.get('BPLID') is not None:
                    assigned_bplids.add(int(assignment.get('BPLID')))

            allowed_centers = []
            for center in centers:
                bpl = center_bpl_map.get(center.id)
                if not assigned_bplids or (bpl is not None and bpl in assigned_bplids) or center.center_name == 'Delhi Main Warehouse':
                    allowed_centers.append(center)
            
            allowed_center_ids = [c.id for c in allowed_centers]
            Vendor.objects.filter(vendor_code=card_code).exclude(center_id__in=allowed_center_ids).filter(purchase_orders__isnull=True).delete()

            for center in allowed_centers:
                vendor, created = Vendor.objects.get_or_create(
                    vendor_code=card_code,
                    center=center,
                    defaults={
                        'name': card_name or card_code,
                        'phone': phone or '',
                        'email': email or '',
                        'sap_sync_status': True
                    }
                )
                if created:
                    logger.info(f"SAP pull: created Vendor {vendor.id} in {center.center_name} from CardCode {card_code}")
                else:
                    update = {}
                    if card_name and vendor.name != card_name:
                        update['name'] = card_name
                    if phone and vendor.phone != phone:
                        update['phone'] = phone
                    if email and vendor.email != email:
                        update['email'] = email
                    if update:
                        Vendor.objects.filter(pk=vendor.pk).update(**update)
                        updated_vendors += 1

        self.stdout.write(
            f"  -> Updated {updated_clients} client(s), {updated_vendors} vendor(s) from SAP."
        )

    # ─── Items ────────────────────────────────────────────────────────────────

    def _item_groups(self):
        """{SAP item group number: group name}, e.g. {101: 'RETAIL', 122: 'TECHNICAL'}."""
        from inventory import sap_items
        from pos_backend import hana_reads
        if hana_reads.available():
            try:
                return sap_items.item_groups()
            except hana_reads.HanaUnavailable as exc:
                logger.warning(f"SAP pull: HANA could not be read ({exc}); using the Service Layer.")
        resp = _send_sap_request('GET', f"{SAP_URL_BASE}/ItemGroups?$select=Number,GroupName", timeout=30)
        return {g['Number']: g['GroupName'] for g in resp.json().get('value', [])} if resp.status_code == 200 else {}

    def _master_items(self, base_url, centers, price_list):
        """Every SAP item with the fields below, the stock of the branches' warehouses and its prices.

        Straight from HANA (a fraction of a second); the Service Layer needs 22 pages of 100 items at about
        five seconds each, so it is only the fallback for when HANA cannot be read."""
        from inventory import sap_items
        from pos_backend import hana_reads
        if hana_reads.available():
            try:
                return iter(sap_items.master_items([c.sap_warehouse_code for c in centers], price_list))
            except hana_reads.HanaUnavailable as exc:
                logger.warning(f"SAP pull: HANA could not be read ({exc}); using the Service Layer.")
        return self._paginate(base_url, 'ItemCode')

    def _sync_items(self):
        self.stdout.write("Pulling Items from SAP...")
        updated = 0
        created_products = 0
        created_services = 0

        import datetime
        # SAP B1 OData $filter on date fields requires datetime format,
        # not a plain string. U_Price is a UDF and cannot be used in $select.
        today_dt = datetime.date.today().strftime('%Y-%m-%dT00:00:00')
        # SAP does not update the Item's UpdateDate when only stock changes (e.g. via Goods Receipt).
        # We must pull all items to ensure stock is synced.
        # Only the columns read below: the whole item record, with every
        # warehouse's stock, is several times the size.
        base_url = (f"{SAP_URL_BASE}/Items?$select=ItemCode,ItemName,InventoryItem,SalesItem,"
                    f"BarCode,ItemPrices,ItemWarehouseInfoCollection,"
                    f"ItemsGroupCode,U_BRAND,U_SUB_GR,U_TAXRT,ChapterID,SACEntry,InventoryUOM")
        price_list = int(_sap('sap.price_list') or 1)
        groups = self._item_groups()

        from salon_admin.models import Center
        centers = list(Center.objects.filter(is_active=True))
        from services.models import ServiceMaster

        for item in self._master_items(base_url, centers, price_list):
            item_code = item.get('ItemCode', '')
            if not item_code: continue
            
            # Skip marketing items (Value Cards, Memberships, Packages) created by POS:
            # VC/MB/PK and six digits. A product that merely starts with those
            # letters (VCO-SERUM) is a real product.
            if MARKETING_ITEM.match(item_code):
                continue
                
            new_name = (item.get('ItemName') or '').strip()
            is_inventory = item.get('InventoryItem', 'tYES') == 'tYES'
            is_sales = item.get('SalesItem', 'tYES') == 'tYES'
            attrs = item_attributes(item, groups)
            from pos_backend import sap_hsn
            hsn = sap_hsn.hsn_code(item.get('ChapterID'))      # what the bill prints; SAP holds a row number
            sac = sap_hsn.sac_code(item.get('SACEntry'))
            # The category is SAP's item group (Retail, Technical, Consumable, Tool ...): it decides
            # whether a product is sold or used inside a service. Every item is "sold" in SAP, so the
            # old "sales item = Retail" guess made everything Retail.
            mapped_category = attrs['category'] or ("Retail" if is_sales else "Technical")
            from services import units
            sap_unit = units.norm(item.get('InventoryUOM'))[:20] or 'pcs'      # SAP: PCS, PACK ...

            if is_inventory:
                # Update Products
                product_qs = Product.objects.filter(product_code=item_code)
                
                # Check stock
                wh_infos = item.get('ItemWarehouseInfoCollection', [])
                wh_stock = {wh.get('WarehouseCode'): float(wh.get('InStock') or 0) for wh in wh_infos}
                
                barcode = item.get('BarCode') or ''
                price = 0.0
                for pr in item.get('ItemPrices', []):
                    if pr.get('PriceList') == price_list and pr.get('Price') is not None:
                        price = float(pr.get('Price'))
                        break

                existing_centers = set(product_qs.values_list('center_id', flat=True))
                
                # 1. Create missing products
                missing_products = []
                for center in centers:
                    if center.id not in existing_centers:
                        stock = wh_stock.get(center.sap_warehouse_code, 0)
                        missing_products.append(Product(
                            product_code=item_code,
                            name=new_name,
                            barcode=barcode,
                            price=price,
                            center=center,
                            current_stock=stock,
                            sap_sync_status=True,
                            category=mapped_category,
                            brand=attrs['brand'] or None,
                            sub_category=attrs['sub_category'] or None,
                            gst_percent=attrs['gst'],
                            sac_code=hsn or None,
                            unit=sap_unit,
                        ))
                if missing_products:
                    Product.objects.bulk_create(missing_products)
                    created_products += len(missing_products)
                    # Refresh product_qs to include newly created ones for any further processing if needed
                    product_qs = Product.objects.filter(product_code=item_code)
                
                # 2. Update existing products
                for p in product_qs:
                    update_fields = {}
                    # Note: We intentionally do NOT overwrite name, price, or stock here.
                    # POS is the source of truth for intraday stock, and managers edit names/prices locally.
                    # If we overwrite them here every 5 minutes, we wipe out their local work.
                    
                    # Only fill a missing category: one a manager chose is kept.
                    if not p.category:
                        update_fields['category'] = mapped_category
                    # Other blanks are filled from SAP too; anything a manager typed is kept.
                    if not p.brand and attrs['brand']:
                        update_fields['brand'] = attrs['brand']
                    if not p.sub_category and attrs['sub_category']:
                        update_fields['sub_category'] = attrs['sub_category']
                    if not p.gst_percent and attrs['gst']:
                        update_fields['gst_percent'] = attrs['gst']
                    if not p.sac_code and hsn:
                        update_fields['sac_code'] = hsn
                    if p.unit in ('', 'pcs') and sap_unit != 'pcs':
                        update_fields['unit'] = sap_unit

                    if update_fields:
                        Product.objects.filter(pk=p.id).update(**update_fields)
                        updated += 1
            else:
                # ServiceMaster. Only items SAP sells become sellable services: a
                # purchase-only item (an expense, a consumable) is not a service
                # and used to appear in the catalogue with no price.
                if not is_sales:
                    continue
                service = ServiceMaster.objects.filter(service_code=item_code).first()
                if not service:
                    ServiceMaster.objects.create(
                        service_code=item_code,
                        name=new_name,
                        category=attrs['category'] or "Imported from SAP",
                        sub_category=attrs['sub_category'] or None,
                        tax_percentage=attrs['gst'],
                        sac_code=sac or None,
                        sap_sync_status=True,           # it came from SAP: never sent back
                    )
                    created_services += 1
                else:
                    fix = {}
                    if new_name and service.name != new_name:
                        fix['name'] = new_name
                    if attrs['category'] and service.category in ('', None, 'Imported from SAP'):
                        fix['category'] = attrs['category']
                    if attrs['sub_category'] and not service.sub_category:
                        fix['sub_category'] = attrs['sub_category']
                    if sac and not service.sac_code:
                        fix['sac_code'] = sac
                    if item.get('U_TAXRT') not in (None, '') and service.tax_percentage != attrs['gst']:
                        fix['tax_percentage'] = attrs['gst']
                    if fix:
                        for field, value in fix.items():
                            setattr(service, field, value)
                        service.save(update_fields=list(fix))
                        updated += 1

        self.stdout.write(f"  -> Updated {updated} items, created {created_products} products and {created_services} services from SAP.")

    # ─── BOMs (Product Trees) ─────────────────────────────────────────────────

    def _sync_boms(self):
        self.stdout.write("Pulling Sales BOMs from SAP...")
        from services.models import ServiceMaster, ServiceBOM
        
        # ProductTrees returns the BOMs. We only care about Sales BOMs (iSalesTree).
        base_url = f"{SAP_URL_BASE}/ProductTrees"
        
        boms_created = 0
        boms_deleted = 0

        for sap_bom in self._paginate(base_url, 'TreeCode'):
            if sap_bom.get('TreeType') != 'iSalesTree':
                continue
            
            tree_code = sap_bom.get('TreeCode')
            if not tree_code: continue

            # Try to find the matching ServiceMaster locally
            # Services are synced to SAP as ItemCode = service_code or f"S{service.id:06d}"
            service = None
            if tree_code.startswith('S') and tree_code[1:].isdigit():
                service = ServiceMaster.objects.filter(id=int(tree_code[1:])).first()
            if not service:
                service = ServiceMaster.objects.filter(service_code=tree_code).first()
            
            if not service:
                continue
            # A recipe made in the POS (Admin > Services > Recipe) is never overwritten.
            if service.recipe_source == 'pos':
                continue

            # Clear existing BOM lines for this service to resync them fresh
            boms_deleted += ServiceBOM.objects.filter(service=service).delete()[0]

            seen_items = {}
            for line in sap_bom.get('ProductTreeLines', []):
                item_code = line.get('ItemCode')
                qty = line.get('Quantity', 0)
                if item_code and qty > 0:
                    seen_items[item_code] = seen_items.get(item_code, 0) + qty
                    
            for item_code, qty in seen_items.items():
                ServiceBOM.objects.create(
                    service=service,
                    product_code=item_code,
                    quantity=qty
                )
                boms_created += 1

        self.stdout.write(f"  -> Synced SAP BOMs: {boms_created} lines created, {boms_deleted} lines replaced.")
