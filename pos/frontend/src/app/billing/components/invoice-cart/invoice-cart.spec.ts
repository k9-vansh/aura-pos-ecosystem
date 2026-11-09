import { ComponentFixture, TestBed } from '@angular/core/testing';

import { InvoiceCart } from './invoice-cart';

describe('InvoiceCart', () => {
  let component: InvoiceCart;
  let fixture: ComponentFixture<InvoiceCart>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InvoiceCart],
    }).compileComponents();

    fixture = TestBed.createComponent(InvoiceCart);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
