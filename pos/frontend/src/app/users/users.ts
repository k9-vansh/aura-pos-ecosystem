import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { ToastService } from '../services/toast.service';
import { todayLocal } from '../services/date-util';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-users',
  imports: [CommonModule, FormsModule],
  templateUrl: './users.html',
  styleUrl: './users.css'
})
export class UsersComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  toastService = inject(ToastService);

  apiService = inject(ApiService);
  cdr = inject(ChangeDetectorRef);
  users: any[] = [];
  currentPage: number = 1;
  pageSize: number = 500;

  get paginatedItems() {
    const startIndex = (this.currentPage - 1) * this.pageSize;
    return this.users.slice(startIndex, startIndex + this.pageSize);
  }

  get totalPages() {
    return Math.ceil(this.users.length / this.pageSize) || 1;
  }

  nextPage() {
    if (this.currentPage < this.totalPages) {
      this.currentPage++;
    }
  }

  prevPage() {
    if (this.currentPage > 1) {
      this.currentPage--;
    }
  }
  roles: any[] = [];
  centers: any[] = [];
  showModal = false;
  isEditing = false;
  editingId: number | null = null;
  isSaving = false;
  
  newUser: any = {
    email: '',
    full_name: '',
    phone: '',
    designation: '',
    password: '',
    role: null,
    center: null,
    centers: []
  };

  isOwner = false;
  /** Changing an existing user's password is an action tick; the server enforces it too. */
  get canResetPassword(): boolean {
    return this.isOwner || this.permissions?.admin?.users?.actions?.reset_password === true;
  }
  hasGlobalAccess = false;
  permissions: any = {};

  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
      } catch (e) {}
    }
    this.loadUsers();
    this.loadRoles();
    this.loadCenters();
  }

  loadUsers() {
    this.apiService.getUsers().pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      this.users = Array.isArray(data) ? data : (data.results || []);
        this.currentPage = 1;
      this.cdr.detectChanges();
    });
  }

  loadRoles() {
    this.apiService.getRoles().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.roles = data;
      this.cdr.detectChanges();
    });
  }

  loadCenters() {
    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.centers = data;
      this.cdr.detectChanges();
    });
  }

  openModal() {
    this.showModal = true;
    this.isEditing = false;
    this.resetForm();
  }

  editUser(user: any) {
    this.isEditing = true;
    this.editingId = user.id;
    this.newUser = { ...user, password: '', centers: user.centers ? [...user.centers] : (user.center ? [user.center] : []) }; 
    this.showModal = true;
  }

  closeModal() {
    this.showModal = false;
    this.resetForm();
  }

  async toggleUserStatus(user: any) {
    if (await this.confirmDialog.ask(`Are you sure you want to ${user.is_active ? 'deactivate' : 'activate'} ${user.full_name}?`)) {
      this.apiService.updateUser(user.id, { is_active: !user.is_active }).subscribe({
        next: () => {
          user.is_active = !user.is_active;
          this.toastService.showSuccess(`User ${user.is_active ? 'activated' : 'deactivated'} successfully`);
        },
        error: (err) => {
          console.error(err);
          this.toastService.showError('Failed to update user status.');
        }
      });
    }
  }

  resetForm() {
    this.newUser = {
      email: '',
      full_name: '',
      phone: '',
      designation: '',
      password: '',
      role: null,
      center: null,
      centers: []
    };
    this.isEditing = false;
    this.editingId = null;
  }

  addCenterFromSelect(value: string) {
    if (!value) return;
    if (!this.newUser.centers) this.newUser.centers = [];

    if (value === 'all') {
      this.newUser.centers = this.centers.map(c => c.id);
      return;
    }

    const centerId = parseInt(value, 10);
    if (!isNaN(centerId) && !this.newUser.centers.includes(centerId)) {
      this.newUser.centers.push(centerId);
    }
  }

  removeCenter(centerId: number) {
    if (!this.newUser.centers) return;
    this.newUser.centers = this.newUser.centers.filter((id: number) => id !== centerId);
  }

  clearCenters() { this.newUser.centers = []; }

  /** Every centre is picked: shown as one "All centers" pill. */
  get allCentersPicked(): boolean {
    const picked = this.newUser.centers || [];
    return this.centers.length > 1 && this.centers.every(c => picked.includes(c.id));
  }

  getCenterName(id: number): string {
    const center = this.centers.find(c => c.id === id);
    return center ? center.center_name : 'Unknown Center';
  }

  getRoleName(id: number): string {
    if (!id) return 'No Role';
    const role = this.roles.find(r => r.id === id);
    return role ? role.name : `Role ID: ${id}`;
  }

  onSubmit() {
    if (this.isSaving) return;
    // A user needs a role (what they may do) and a centre (whose records they see).
    if (!this.newUser.role) {
      this.toastService.showError('Choose a role for this user.');
      return;
    }
    if (!this.newUser.centers || this.newUser.centers.length === 0) {
      this.toastService.showError('Choose at least one centre for this user.');
      return;
    }
    this.isSaving = true;

    const payload: any = { ...this.newUser };
    if (!payload.center || !payload.centers.includes(payload.center)) payload.center = payload.centers[0];
    // The plain user update never changes a password (the server ignores it): a typed password goes through the
    // reset-password call once the rest is saved.
    const newPassword: string = this.isEditing ? (payload.password || '') : '';
    if (this.isEditing) {
      delete payload.password;
    }
    
    if (this.isEditing && this.editingId) {
      const editedId = this.editingId;
      this.apiService.updateUser(editedId, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          if (!newPassword) {
            this.loadUsers();
            this.closeModal();
            this.isSaving = false;
            return;
          }
          this.apiService.resetUserPassword(editedId, newPassword).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
            next: () => {
              this.toastService.showSuccess('Saved. The new password works from the next sign-in.');
              this.loadUsers();
              this.closeModal();
              this.isSaving = false;
            },
            error: (err) => {
              const e = err?.error || {};
              const pw = e.new_password;
              this.toastService.showError('Details saved, but the password was not changed: ' +
                (Array.isArray(pw) ? pw.join(' ') : (pw || e.detail || 'try again')));
              this.loadUsers();
              this.isSaving = false;
            }
          });
        },
        error: (err) => {
          let errMsg = 'Failed to update user';
          if (err.error) {
             if (typeof err.error === 'string') errMsg = err.error;
             else if (typeof err.error === 'object') {
                const msgs: string[] = [];
                for (const key in err.error) {
                   const val = err.error[key];
                   msgs.push(`${key}: ${Array.isArray(val) ? val.join(' ') : val}`);
                }
                errMsg = msgs.join(' | ');
             }
          }
          this.toastService.showError(errMsg);
          this.isSaving = false;
        }
      });
    } else {
      this.apiService.createUser(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadUsers();
          this.closeModal();
          this.isSaving = false;
        },
        error: (err) => {
          let errMsg = 'Failed to create user';
          if (err.error) {
             if (typeof err.error === 'string') errMsg = err.error;
             else if (typeof err.error === 'object') {
                const msgs: string[] = [];
                for (const key in err.error) {
                   const val = err.error[key];
                   msgs.push(`${key}: ${Array.isArray(val) ? val.join(' ') : val}`);
                }
                errMsg = msgs.join(' | ');
             }
          }
          this.toastService.showError(errMsg);
          this.isSaving = false;
        }
      });
    }
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
