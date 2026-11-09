import { Component, Input, Output, EventEmitter, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-held-carts-modal',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './held-carts-modal.html'
})
export class HeldCartsModalComponent {
  @Input() show: boolean = false;
  @Input() heldCarts: any[] = [];
  
  @Output() close = new EventEmitter<void>();
  @Output() onDelete = new EventEmitter<any>();
  @Output() onResume = new EventEmitter<any>();
}
