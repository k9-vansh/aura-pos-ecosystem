import { Component, HostListener, inject, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ConfirmService } from '../../services/confirm.service';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-confirm',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './confirm.html',
  styleUrl: './confirm.css'
})
export class ConfirmComponent {
  svc = inject(ConfirmService);

  @HostListener('document:keydown.escape')
  onEscape() {
    if (this.svc.request()) this.svc.answer(false);
  }

  submit(req: { input?: { value: string } }) {
    if (req.input) {
      const text = (req.input.value || '').trim();
      this.svc.answer(text ? text : null);
    } else {
      this.svc.answer(true);
    }
  }
}
