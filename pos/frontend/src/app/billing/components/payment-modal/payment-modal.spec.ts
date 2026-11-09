import { ComponentFixture, TestBed } from '@angular/core/testing';

import { PaymentModalComponent } from './payment-modal';

describe('PaymentModalComponent', () => {
  let component: PaymentModalComponent;
  let fixture: ComponentFixture<PaymentModalComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PaymentModalComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(PaymentModalComponent);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});

describe('PaymentModalComponent: pressing inside the dialog', () => {
  let fixture: ComponentFixture<PaymentModalComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [PaymentModalComponent] }).compileComponents();
    fixture = TestBed.createComponent(PaymentModalComponent);
    fixture.componentInstance.show = true;
    fixture.componentInstance.checkoutPayments = [{ method: 'Cash', amount: 15750 }, { method: 'UPI', amount: 0 }];
    fixture.detectChanges();
  });

  /** A press the browser may act on: if anything calls preventDefault on it, the box never gets the cursor. */
  function press(el: Element): MouseEvent {
    const event = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    el.dispatchEvent(event);
    return event;
  }

  it('lets the browser put the cursor in an amount box (the press is not cancelled)', () => {
    const boxes = fixture.nativeElement.querySelectorAll('input[type="number"]') as NodeListOf<HTMLInputElement>;
    expect(boxes.length).toBeGreaterThan(1);
    boxes.forEach(box => expect(press(box).defaultPrevented).toBe(false));
  });

  it('lets the browser put the cursor in the reference box too', () => {
    const reference = fixture.nativeElement.querySelector('input[type="text"]') as HTMLInputElement;
    expect(press(reference).defaultPrevented).toBe(false);
  });

  it('a press on the dark backdrop is remembered, so releasing there closes the dialog', () => {
    const overlay = fixture.nativeElement.querySelector('.modal-overlay') as HTMLElement;
    press(overlay);
    expect(fixture.componentInstance.overlayPressed).toBe(true);
    press(fixture.nativeElement.querySelector('input[type="number"]'));
    expect(fixture.componentInstance.overlayPressed).toBe(false);
  });
});
