import { Component, Input, Output, EventEmitter, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule, DatePipe } from '@angular/common';
import { todayLocal } from '../../../services/date-util';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-billing-landing',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './billing-landing.component.html',
  styleUrls: ['../../billing.css'],
  styles: [`
    :host {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      width: 100%;
      height: 100%;
      padding: 4px 8px 12px 8px;
      box-sizing: border-box;
    }

    @media (max-width: 880px) {
      :host {
        grid-template-columns: repeat(2, 1fr);
        height: auto;
      }
    }

    @media (max-width: 480px) {
      :host {
        grid-template-columns: 1fr;
      }
    }
    
    .landing-col {
      background: #ffffff;
      border-radius: 10px;
      border: 1px solid #e2e8f0;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
      display: flex;
      flex-direction: column;
      padding: 12px 14px;
      height: 100%;
      overflow: hidden;
      transition: all 0.2s ease;
    }
    
    .landing-col:hover {
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
      border-color: #cbd5e1;
    }
    
    .landing-header-new {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
      font-size: 13.5px;
      font-weight: 700;
      color: #0f172a;
      border-bottom: 1px solid #f1f5f9;
      padding-bottom: 8px;
      background: transparent;
      flex-shrink: 0;
    }
    
    .header-icon {
      width: 24px;
      height: 24px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #ffffff;
      font-size: 12px;
      flex-shrink: 0;
    }

    .header-icon svg {
      width: 14px;
      height: 14px;
    }
    
    .icon-invoice { background: var(--blue, #2563eb); }
    .icon-appt { background: var(--orange, #ea580c); }
    .icon-staff { background: var(--green, #16a34a); }
    .icon-live { background: var(--green, #16a34a); }

    /* Premium Dark Mode Overrides */
    :host-context([data-theme="dark"]) .landing-col {
      background: linear-gradient(180deg, #18181b 0%, #0f0f11 100%) !important;
      border: 1px solid rgba(255, 255, 255, 0.06) !important;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04), 0 10px 30px rgba(0, 0, 0, 0.6) !important;
    }

    :host-context([data-theme="dark"]) .landing-col:hover {
      background: linear-gradient(180deg, #1e1e22 0%, #111114 100%) !important;
      border-color: rgba(255, 255, 255, 0.12) !important;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 12px 40px rgba(0, 0, 0, 0.8) !important;
    }

    :host-context([data-theme="dark"]) .landing-header-new {
      color: #ffffff;
      border-bottom-color: rgba(255, 255, 255, 0.06);
    }

    :host-context([data-theme="dark"]) .icon-invoice { background: rgba(37, 99, 235, 0.15); color: #60a5fa; }
    :host-context([data-theme="dark"]) .icon-appt { background: rgba(234, 88, 12, 0.15); color: #fb923c; }
    :host-context([data-theme="dark"]) .icon-staff { background: rgba(22, 163, 74, 0.15); color: #4ade80; }
    :host-context([data-theme="dark"]) .icon-live { background: rgba(22, 163, 74, 0.15); color: #4ade80; }

    :host-context([data-theme="dark"]) .landing-item {
      background: #111111 !important;
      border: 1px solid rgba(255, 255, 255, 0.06) !important;
    }

    :host-context([data-theme="dark"]) .landing-item:hover {
      background: #1a1a1a !important;
      border-color: rgba(255, 255, 255, 0.15) !important;
    }

    
    .landing-list {
      flex: 1;
      overflow-y: auto;
      padding: 4px 2px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .landing-list::-webkit-scrollbar {
      width: 4px;
    }
    .landing-list::-webkit-scrollbar-thumb {
      background-color: #cbd5e1;
      border-radius: 6px;
    }
    
    .landing-item {
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 8px 10px;
      transition: all 0.15s ease;
      cursor: pointer;
    }
    
    .landing-item:hover {
      border-color: #94a3b8;
      background: #f1f5f9;
      transform: translateY(-1px);
    }
    
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      flex: 1;
      color: #94a3b8;
      font-weight: 500;
      font-size: 12px;
      gap: 8px;
      padding: 24px 8px;
      text-align: center;
    }
    .empty-state i {
      font-size: 22px;
      color: #cbd5e1;
    }
    
    /* DARK MODE OVERRIDES */
    :host-context([data-theme="dark"]) .landing-col {
      background: #111111;
      border-color: rgba(255, 255, 255, 0.07);
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
    }
    :host-context([data-theme="dark"]) .landing-col:hover {
      border-color: rgba(255, 255, 255, 0.15);
    }
    :host-context([data-theme="dark"]) .landing-header-new {
      color: #e2e8f0;
      border-bottom-color: rgba(255, 255, 255, 0.05);
    }
    :host-context([data-theme="dark"]) .landing-item {
      background: #1a1a1a;
      border-color: rgba(255, 255, 255, 0.05);
    }
    :host-context([data-theme="dark"]) .landing-item:hover {
      background: #222222;
      border-color: rgba(255, 255, 255, 0.1);
    }
    :host-context([data-theme="dark"]) .empty-state {
      color: #64748b;
    }
    :host-context([data-theme="dark"]) .empty-state i {
      color: #334155;
    }
  `]
})
export class BillingLandingComponent {
  todayDate: string = todayLocal();
  @Input() globalInvoices: any[] = [];
  @Input() appointments: any[] = [];
  @Input() staffActivity: any[] = [];
  @Input() activityFeed: any[] = [];
  @Input() staffMembers: any[] = [];
  
  @Output() onInvoiceClick = new EventEmitter<any>();
  @Output() onDeleteInvoice = new EventEmitter<any>();

  getStaffName(staffId: number | string): string {
    const s = this.staffMembers.find(x => x.id === Number(staffId));
    return s ? `${s.first_name} ${s.last_name}` : 'Unknown';
  }
}
