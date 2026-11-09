import { AdminManagerDiscountsComponent } from './admin-manager-discounts/admin-manager-discounts';
import { AdminBillsComponent } from './admin-bills/admin-bills';

const md = AdminManagerDiscountsComponent.prototype as any;
const bills = AdminBillsComponent.prototype as any;

describe('who raised a bill, and how a discount is described', () => {
  it('names the person who raised the bill, not "Admin"', () => {
    expect(md.billedBy({ created_by_name: 'Ravi Kumar', staff: 4 })).toBe('Ravi Kumar');
    expect(bills.getBiller({ created_by_name: 'Ravi Kumar' })).toBe('Ravi Kumar');
  });

  it('falls back to the line stylist, then to a dash', () => {
    expect(md.billedBy({ staff: 4 })).toBe('Staff #4');
    expect(md.billedBy({})).toBe('-');
    expect(bills.getBiller({})).toBe('-');
  });

  it('writes the item type as a word', () => {
    expect(md.typeLabel({ content_type: 'services.servicemaster' })).toBe('Service');
    expect(md.typeLabel({ content_type: 'inventory.product' })).toBe('Product');
    expect(md.typeLabel({ content_type: 'billing.advancepayment' })).toBe('Advance');
    expect(md.typeLabel({})).toBe('Service');
  });

  it('works the discount percent on the whole line, not one unit', () => {
    expect(md.itemPercent({ unit_price: '500', quantity: 1 }, 250)).toBe(50);
    expect(md.itemPercent({ unit_price: '500', quantity: 2 }, 100)).toBe(10);
    expect(md.itemPercent({ unit_price: '0', quantity: 2 }, 100)).toBe(0);
  });
});
