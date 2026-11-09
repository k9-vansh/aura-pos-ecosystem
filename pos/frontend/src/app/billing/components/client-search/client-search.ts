import { Component, Input, Output, EventEmitter, inject, DestroyRef, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../../services/api';
import { ToastService } from '../../../services/toast.service';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CLIENT_SEARCH_HINT, canSearchClients } from '../../../services/client-search-rule';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-client-search',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './client-search.html'
})
export class ClientSearchComponent {
  private apiService = inject(ApiService);
  private toastService = inject(ToastService);
  private destroyRef = inject(DestroyRef);
  private cdr = inject(ChangeDetectorRef);

  @Input() clientSelected: boolean = false;
  
  @Output() onSelectClient = new EventEmitter<any>();
  @Output() newClient = new EventEmitter<void>();
  @Output() anonymousClient = new EventEmitter<void>();

  searchPhone: string = '';
  clients: any[] = [];
  isLoading: boolean = false;
  /** Typed something, but not enough for the server to search yet. */
  tooShort: boolean = false;
  readonly hint = CLIENT_SEARCH_HINT;
  private searchTimeout: any;

  onSearchPhoneChange() {
    if (this.searchTimeout) clearTimeout(this.searchTimeout);

    const searchable = canSearchClients(this.searchPhone);
    this.tooShort = this.searchPhone.trim().length > 0 && !searchable;
    if (searchable) {
      this.isLoading = true;
    } else {
      this.isLoading = false;
      this.clients = [];
    }
    this.cdr.detectChanges();

    this.searchTimeout = setTimeout(() => {
      if (canSearchClients(this.searchPhone)) {
        this.searchClients();
      }
    }, 150);
  }

  searchClients() {
    const currentSearch = this.searchPhone;
    if (!canSearchClients(currentSearch)) {
      this.clients = [];
      this.isLoading = false;
      this.tooShort = currentSearch.trim().length > 0;
      this.cdr.detectChanges();
      return;
    }
    this.apiService.get('clients/api/clients/', { q: currentSearch })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        // Prevent race condition: if the user typed something else while this request was in flight, ignore the result.
        if (this.searchPhone !== currentSearch) return;

        const d = res && res.results ? res.results : res;
        this.clients = d || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      }, error: (err: any) => {
        if (this.searchPhone !== currentSearch) return;
        console.error('Clients API error', err);
        this.isLoading = false;
        this.cdr.detectChanges();
      }
    });
  }

  selectClient(client: any) {
    if (client.is_blacklisted) {
      this.toastService.show('This client is blacklisted and cannot be billed.', 'error');
      this.searchPhone = '';
      this.clients = [];
      this.cdr.detectChanges();
      return;
    }
    this.searchPhone = '';
    this.clients = [];
    this.isLoading = false;
    this.tooShort = false;
    this.cdr.detectChanges();
    this.onSelectClient.emit(client);
  }

  trackById(index: number, item: any): number {
    return item.id;
  }
}
