import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AdminFilterService } from '../admin-filter.service';

/** Date range, Apply and Export for the admin pages that report over a period. */
@Component({
  selector: 'app-admin-range-bar',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-range-bar.html',
  styleUrl: './admin-range-bar.css'
})
export class AdminRangeBarComponent {
  svc = inject(AdminFilterService);
}
