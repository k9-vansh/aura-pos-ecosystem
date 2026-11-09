import { Component, Input, Output, EventEmitter, ChangeDetectionStrategy, ChangeDetectorRef, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../../services/api';
import { giftCardProblem, normaliseCouponCode } from '../../../services/payment-split';

export interface PaymentOption { value: string; label: string; sub?: string; icon?: string; }

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-payment-modal',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './payment-modal.html'
})
export class PaymentModalComponent {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);

  @Input() show: boolean = false;
  /** What is to be paid now (the bill less any value card's part). */
  @Input() finalTotalAmount: number = 0;
  /** The value card's part of the bill, settled from the card (F5). */
  @Input() cardSettled: number = 0;
  /** The ways this branch is paid, from Admin > Settings. */
  @Input() paymentMethods: string[] = ['Cash', 'UPI', 'Card', 'Cheque'];
  @Input() checkoutPayments: Record<string, any>[] = [];
  @Input() clientAdvanceBalance: number = 0;
  @Input() clientCashbackBalance: number = 0;
  @Input() clientValueCards: any[] = [];
  @Input() checkoutRemaining: number = 0;
  @Input() canCompleteSale: boolean = false;
  @Input() isSaving: boolean = false;
  @Input() client: any = null;
  /** The branch the bill is for: a coupon is valid at some branches only. */
  @Input() centerId: number | null = null;

  /** True only while a press that started on the backdrop itself is in progress.
   *  A drag that starts inside the window and ends on the backdrop must not close checkout. */
  overlayPressed = false;

  /**
   * Remember whether a press began on the backdrop itself. This is a method, not an inline `x = a === b`:
   * an Angular template handler whose value is `false` calls preventDefault(), and the browser then never
   * puts the cursor in the box that was pressed - no field in the dialog could be clicked into.
   */
  pressOverlay(event: Event) {
    this.overlayPressed = event.target === event.currentTarget;
  }

  @Output() close = new EventEmitter<void>();
  @Output() confirm = new EventEmitter<void>();
  @Output() onMethodChange = new EventEmitter<void>();
  @Output() onAmountChange = new EventEmitter<void>();
  @Output() onAddPaymentRow = new EventEmitter<void>();
  @Output() onRemovePaymentRow = new EventEmitter<number>();

  /** The whole bill, value card's part included: a coupon's worth and minimum are worked on it. */
  get billTotal(): number {
    return Math.round((Number(this.finalTotalAmount || 0) + Number(this.cardSettled || 0)) * 100) / 100;
  }

  /** Build the options list for a given payment row index. */
  getPaymentOptions(currentIndex: number): PaymentOption[] {
    const usedMethods = this.checkoutPayments
      .filter((_, i) => i !== currentIndex)
      .map(p => p['method']);

    const opts: PaymentOption[] = (this.paymentMethods || [])
      .map(m => ({ value: m === 'Credit Card' ? 'Card' : m, label: m === 'Credit Card' ? 'Card' : m }));
    opts.push(
      { value: 'Advance',         label: 'Advance / Wallet', sub: `Balance ₹${this.clientAdvanceBalance}` },
      { value: 'Cashback Wallet', label: 'Cashback Wallet',  sub: `Balance ₹${this.clientCashbackBalance}` },
      { value: 'Gift Card',       label: 'Gift Card / Coupon', sub: "A brand's coupon code", icon: 'fa-gift' },
    );
    // A value card is chosen on the bill, where it pays for the services it
    // covers (F5); it is not a payment amount typed in here.

    // Filter out methods already chosen in other rows
    return opts.filter(o => !usedMethods.includes(o.value));
  }

  /**
   * The till may hold an amount back (a wallet row cannot take more than its balance). The box would keep showing
   * what was typed, because the number behind it did not change from its last value, so it is set to the amount
   * that is really used.
   */
  syncAmount(el: HTMLInputElement, p: Record<string, any>) {
    if (Number(el.value || 0) !== Number(p['amount'] || 0)) el.value = String(p['amount'] ?? '');
  }

  /** True while a wallet row asks for more than the balance it draws on: the bill is not really paid then. */
  get walletShort(): boolean {
    return this.checkoutPayments.some(p => {
      const amount = Number(p['amount'] || 0);
      return (p['method'] === 'Advance' && amount > Number(this.clientAdvanceBalance || 0) + 0.004)
        || (p['method'] === 'Cashback Wallet' && amount > Number(this.clientCashbackBalance || 0) + 0.004);
    });
  }

  /** Human-readable label for the currently selected method. */
  getMethodLabel(method: string): string {
    if (!method) return 'Select payment method…';
    if (method === 'Advance')
      return `Advance / Wallet  (Bal: ₹${this.clientAdvanceBalance})`;
    if (method === 'Cashback Wallet')
      return `Cashback Wallet  (Bal: ₹${this.clientCashbackBalance})`;
    if (method === 'Gift Card')
      return 'Gift Card / Coupon';
    if (method.startsWith('Value Card ')) {
      const id = parseInt(method.replace('Value Card ', ''), 10);
      const vc = this.clientValueCards.find(c => c.id === id);
      const title = vc?.value_card_detail?.title || 'Value Card';
      const bal = vc ? vc.balance : '';
      return `💳 ${title}  (Bal: ₹${bal})`;
    }
    return method;
  }

  selectMethod(p: Record<string, any>, value: string) {
    if (p['method'] !== value) {
      // A UPI transaction id or a cheque number is not a coupon code (and the other way round).
      p['reference'] = '';
      this.resetGift(p);
    }
    p['method'] = value;
    if (value !== 'Cash') delete p['tendered'];
  }

  /** Change to hand back on a cash row: cash received less the amount. */
  changeFor(p: Record<string, any>): number {
    return Math.round((Number(p['tendered'] || 0) - Number(p['amount'] || 0)) * 100) / 100;
  }

  /** Online money and cheques carry a reference (UPI id, slip, cheque no.). A coupon has its own box. */
  needsReference(p: Record<string, any>): boolean {
    const m = p['method'];
    return !!m && m !== 'Cash' && m !== 'Advance' && m !== 'Cashback Wallet' && m !== 'Gift Card';
  }

  // ─── Gift card / coupon ──────────────────────────────────────────────

  private resetGift(p: Record<string, any>) {
    delete p['giftStatus'];
    delete p['giftMessage'];
    delete p['giftApplies'];
    delete p['giftChecked'];
    delete p['giftBrand'];
  }

  /** Typing in the code box takes back an earlier "valid": the new text has not been checked. */
  onGiftCodeInput(p: Record<string, any>) {
    if (p['giftChecked'] && p['giftChecked'] !== normaliseCouponCode(p['reference'])) {
      this.resetGift(p);
    } else if (p['giftStatus'] === 'bad') {
      this.resetGift(p);
    }
  }

  /** Ask the server whether this code would pay on this bill, and how much. */
  checkGift(p: Record<string, any>) {
    if (p['giftStatus'] === 'checking') return;
    const code = String(p['reference'] || '').trim();
    if (!code) {
      p['giftStatus'] = 'bad';
      p['giftMessage'] = 'Enter the coupon code.';
      return;
    }
    p['giftStatus'] = 'checking';
    p['giftMessage'] = '';
    this.api.checkGiftCard(code, this.billTotal, this.centerId).subscribe({
      next: (r: any) => {
        if (r?.valid) {
          p['giftStatus'] = 'ok';
          p['giftMessage'] = r.message;
          p['giftApplies'] = Number(r.applies);
          p['giftChecked'] = normaliseCouponCode(code);
          p['giftBrand'] = r.brand || '';
          p['reference'] = r.code || code;
          // Pay with as much of it as the bill can take; the other row gives way.
          p['amount'] = Math.min(Number(r.applies), Number(this.finalTotalAmount || 0));
          p['touched'] = true;
          this.onAmountChange.emit();
        } else {
          p['giftStatus'] = 'bad';
          p['giftMessage'] = r?.message || 'This code cannot be used.';
        }
        this.cdr.markForCheck();
      },
      error: () => {
        p['giftStatus'] = 'bad';
        p['giftMessage'] = 'Could not check the code. Try again.';
        this.cdr.markForCheck();
      },
    });
  }

  /** The row's coupon is fine for now, or what is wrong with it. */
  giftProblem(p: Record<string, any>): string | null {
    return giftCardProblem(p);
  }

  /** The first reason a Gift Card row blocks the sale, shown above the button. */
  get giftIssue(): string | null {
    for (const p of this.checkoutPayments) {
      const problem = giftCardProblem(p);
      if (problem) return problem;
    }
    return null;
  }

  /** The amount typed is more than the checked coupon pays. */
  giftOver(p: Record<string, any>): boolean {
    return p['method'] === 'Gift Card' && p['giftStatus'] === 'ok'
      && Number(p['amount'] || 0) > Number(p['giftApplies'] || 0) + 0.004;
  }
}
