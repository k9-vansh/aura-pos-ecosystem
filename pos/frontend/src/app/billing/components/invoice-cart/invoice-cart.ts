import { Component, ChangeDetectionStrategy } from '@angular/core';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-invoice-cart',
  imports: [],
  templateUrl: './invoice-cart.html',
  styleUrl: './invoice-cart.css',
})
export class InvoiceCart {}
