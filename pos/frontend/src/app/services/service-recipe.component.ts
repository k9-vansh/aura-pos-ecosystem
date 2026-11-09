import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, EventEmitter, Input, OnInit, Output, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api';

interface UnitFields { unit_options: string[]; pack_size: string; pack_unit: string; }
interface FixedRow extends UnitFields { product_code: string; product_name: string; quantity: string; unit: string; }
interface OptionRow extends UnitFields {
  mode: 'product' | 'category';
  product_code: string; product_name: string; category: string;
  quantity: string; unit: string; is_default: boolean;
}
interface GroupRow { name: string; required: boolean; options: OptionRow[]; }

/**
 * The recipe of a service: the products it uses (Admin > Services > Recipe).
 *
 * "Always used" items come out of stock every time. A "client chooses" group is a
 * pick-one slot (Colour, Shampoo): the person billing picks one option per client.
 * An option is one product, or a whole category, so a colour added to the category
 * later is offered with no edit here. Retail products are sold, never used in a
 * service, so they cannot be picked.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-service-recipe',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="sr-overlay" (mousedown)="onOverlay($event)">
      <div class="sr-card" role="dialog" aria-modal="true" aria-label="Service recipe">
        <header class="sr-head">
          <div class="sr-title">
            <div class="sr-eyebrow">Recipe for</div>
            <h2>{{ service?.name }}</h2>
          </div>
          <div class="sr-head-right">
            <span class="sr-chip" [class.pos]="source === 'pos'">{{ source === 'pos' ? 'Made in the POS' : 'From SAP' }}</span>
            <button class="sr-x" (click)="close(false)" aria-label="Close">&times;</button>
          </div>
        </header>

        <div class="sr-body">
          <p class="sr-lede">
            Tell the POS what this service uses. Stock comes off at the branch that does the service, and the items
            used are listed under the service on the SAP invoice. You can add as many choices as you need, for
            example <strong>Colour</strong> with 3 options and <strong>Shampoo</strong> with 4: the person billing
            picks one option from each.
          </p>

          <div class="sr-note warn" *ngIf="source === 'sap' && (fixed.length || groups.length) && canEdit">
            This recipe came from SAP. Once you save here the POS owns it and SAP will no longer change it.
          </div>
          <div class="sr-note" *ngIf="!canEdit">
            A recipe is the same for every branch, so changing it needs an all-branches role. You can look but not change it.
          </div>
          <div class="sr-note err" *ngIf="error">{{ error }}</div>
          <div class="sr-loading" *ngIf="loading">Loading the recipe...</div>

          <ng-container *ngIf="!loading">
            <!-- What the till will ask, in one line -->
            <div class="sr-preview" *ngIf="fixed.length || groups.length">
              <span class="sr-preview-label">On the bill</span>
              <span class="sr-pill" *ngIf="fixed.length">{{ fixed.length }} always used</span>
              <span class="sr-pill pick" *ngFor="let g of groups; trackBy: trackByIndex">
                {{ g.name || 'Unnamed choice' }}: pick 1 of {{ g.options.length }}
              </span>
            </div>

            <!-- 1. Always used -->
            <section class="sr-section">
              <div class="sr-section-head">
                <div class="sr-step">1</div>
                <div class="sr-section-text">
                  <h3>Always used</h3>
                  <p>Taken every time the service is done: scissors, a towel, a cape.</p>
                </div>
                <button class="sr-add" *ngIf="canEdit" (click)="addFixed()">+ Add item</button>
              </div>

              <div class="sr-empty" *ngIf="!fixed.length">Nothing yet. Leave this empty if the service has no fixed items.</div>

              <div class="sr-cols" *ngIf="fixed.length"><span>Product</span><span>How much</span></div>
              <div class="sr-row" *ngFor="let row of fixed; let i = index; trackBy: trackByIndex">
                <div class="sr-picker">
                  <input class="sr-input" [value]="row.product_code ? row.product_name + ' (' + row.product_code + ')' : ''"
                    placeholder="Search a product by name or code" [disabled]="!canEdit"
                    (input)="search('f' + i, $any($event.target).value)" (focus)="search('f' + i, '')"
                    (blur)="closePickerLater()" autocomplete="off" />
                  <div class="sr-menu" *ngIf="pickerKey === 'f' + i">
                    <div class="sr-menu-empty" *ngIf="!results.length">{{ searching ? 'Searching...' : 'No product found' }}</div>
                    <button type="button" class="sr-menu-item" *ngFor="let p of results; trackBy: trackByCode"
                      (mousedown)="pickFixed(i, p)">
                      <strong>{{ p.name }}</strong><span>{{ p.product_code }} &middot; {{ p.category || 'No category' }}</span>
                    </button>
                  </div>
                </div>
                <div class="sr-qty">
                  <input class="sr-input num" type="number" min="0" step="any" [(ngModel)]="row.quantity" [disabled]="!canEdit" />
                  <select class="sr-input sr-unit-select" *ngIf="row.product_code && canEdit" [ngModel]="row.unit" (ngModelChange)="row.unit = $event; onUnitPicked(row)" aria-label="Unit">
                    <option *ngFor="let u of unitChoices(row)" [value]="u">{{ u }}</option>
                  </select>
                  <span class="sr-unit" *ngIf="!row.product_code || !canEdit">{{ row.unit || 'qty' }}</span>
                </div>
                <div class="sr-pack" *ngIf="needsPack(row) && canEdit">
                  1 {{ stockUnit(row) }} holds
                  <input class="sr-input num sm" type="number" min="0" step="any" [(ngModel)]="row.pack_size" />
                  <select class="sr-input sm" [(ngModel)]="row.pack_unit"><option *ngFor="let u of packUnits" [value]="u">{{ u }}</option></select>
                </div>
                <button class="sr-del" *ngIf="canEdit" (click)="fixed.splice(i, 1)" aria-label="Remove" title="Remove">&times;</button>
              </div>
            </section>

            <!-- 2. Client chooses -->
            <section class="sr-section">
              <div class="sr-section-head">
                <div class="sr-step">2</div>
                <div class="sr-section-text">
                  <h3>Client chooses</h3>
                  <p>A slot where the client picks one, like Colour A, B or C. Add one choice for each thing the client picks.</p>
                </div>
              </div>

              <div class="sr-empty" *ngIf="!groups.length">No choices yet. Add one if the client picks a colour, a shampoo or similar.</div>

              <div class="sr-group" *ngFor="let g of groups; let gi = index; trackBy: trackByIndex">
                <div class="sr-group-head">
                  <span class="sr-group-tag">Choice {{ gi + 1 }}</span>
                  <input class="sr-input sr-group-name" [(ngModel)]="g.name" placeholder="Name it, for example Colour or Shampoo" [disabled]="!canEdit" />
                  <label class="sr-switch" [class.disabled]="!canEdit">
                    <input type="checkbox" [(ngModel)]="g.required" [disabled]="!canEdit" />
                    <span class="sr-track"><span class="sr-knob"></span></span>
                    <span class="sr-switch-text">{{ g.required ? 'Must be chosen' : 'Optional' }}</span>
                  </label>
                  <button class="sr-remove" *ngIf="canEdit" (click)="groups.splice(gi, 1)">Remove choice</button>
                </div>

                <div class="sr-cols opt" *ngIf="g.options.length">
                  <span>Options the client can pick from ({{ g.options.length }})</span><span>How much</span><span>Default</span>
                </div>
                <div class="sr-opt" *ngFor="let o of g.options; let oi = index; trackBy: trackByIndex">
                  <div class="sr-mode" *ngIf="canEdit">
                    <button type="button" [class.on]="o.mode === 'product'" (click)="setMode(o, 'product')">A product</button>
                    <button type="button" [class.on]="o.mode === 'category'" (click)="setMode(o, 'category')">A category</button>
                  </div>

                  <div class="sr-picker" *ngIf="o.mode === 'product'">
                    <input class="sr-input" [value]="o.product_code ? o.product_name + ' (' + o.product_code + ')' : ''"
                      placeholder="Search a product" [disabled]="!canEdit"
                      (input)="search('g' + gi + 'o' + oi, $any($event.target).value)"
                      (focus)="search('g' + gi + 'o' + oi, '')" (blur)="closePickerLater()" autocomplete="off" />
                    <div class="sr-menu" *ngIf="pickerKey === 'g' + gi + 'o' + oi">
                      <div class="sr-menu-empty" *ngIf="!results.length">{{ searching ? 'Searching...' : 'No product found' }}</div>
                      <button type="button" class="sr-menu-item" *ngFor="let p of results; trackBy: trackByCode"
                        (mousedown)="pickOption(o, p)">
                        <strong>{{ p.name }}</strong><span>{{ p.product_code }} &middot; {{ p.category || 'No category' }}</span>
                      </button>
                    </div>
                  </div>

                  <div class="sr-picker" *ngIf="o.mode === 'category'">
                    <select class="sr-input" [(ngModel)]="o.category" [disabled]="!canEdit">
                      <option value="">Choose a category</option>
                      <option *ngFor="let c of categories" [value]="c">Every product in {{ c }}</option>
                    </select>
                  </div>

                  <div class="sr-qty">
                    <input class="sr-input num" type="number" min="0" step="any" [(ngModel)]="o.quantity" [disabled]="!canEdit" />
                    <select class="sr-input sr-unit-select" *ngIf="o.mode === 'product' && o.product_code && canEdit" [ngModel]="o.unit" (ngModelChange)="o.unit = $event; onUnitPicked(o)" aria-label="Unit">
                      <option *ngFor="let u of unitChoices(o)" [value]="u">{{ u }}</option>
                    </select>
                    <span class="sr-unit" *ngIf="o.mode !== 'product' || !o.product_code || !canEdit">{{ o.mode === 'category' ? 'own unit' : (o.unit || 'qty') }}</span>
                  </div>
                  <div class="sr-pack" *ngIf="needsPack(o) && canEdit">
                    1 {{ stockUnit(o) }} holds
                    <input class="sr-input num sm" type="number" min="0" step="any" [(ngModel)]="o.pack_size" />
                    <select class="sr-input sm" [(ngModel)]="o.pack_unit"><option *ngFor="let u of packUnits" [value]="u">{{ u }}</option></select>
                  </div>

                  <label class="sr-default" title="Picked for the client unless the person billing chooses another"
                    [class.disabled]="!canEdit" [class.on]="o.is_default">
                    <input type="radio" [name]="'default-' + gi" [checked]="o.is_default" [disabled]="!canEdit"
                      (click)="toggleDefault(g, o)" />
                    <span>{{ o.is_default ? 'Default' : 'Make default' }}</span>
                  </label>
                  <button class="sr-del" *ngIf="canEdit" (click)="g.options.splice(oi, 1)" aria-label="Remove option" title="Remove option">&times;</button>
                </div>

                <button class="sr-add-option" *ngIf="canEdit" (click)="addOption(g)">+ Add option to {{ g.name || 'this choice' }}</button>
              </div>

              <button class="sr-add-group" *ngIf="canEdit" (click)="addGroup()">
                + Add {{ groups.length ? 'another' : 'a' }} choice
                <span>{{ groups.length ? 'for example Shampoo, Serum, Treatment' : 'for example Colour or Shampoo' }}</span>
              </button>
            </section>
          </ng-container>
        </div>

        <footer class="sr-foot">
          <span class="sr-foot-note" *ngIf="!loading">
            {{ fixed.length }} always used &middot; {{ groups.length }} {{ groups.length === 1 ? 'choice' : 'choices' }}
          </span>
          <span class="sr-spacer"></span>
          <button class="sr-btn ghost" (click)="close(false)">{{ canEdit ? 'Cancel' : 'Close' }}</button>
          <button class="sr-btn primary" *ngIf="canEdit" [disabled]="saving || loading" (click)="save()">
            {{ saving ? 'Saving...' : 'Save recipe' }}
          </button>
        </footer>
      </div>
    </div>
  `,
  styles: [`
    .sr-overlay { position: fixed; inset: 0; z-index: 3000; display: flex; align-items: center; justify-content: center;
      padding: 24px; background: rgba(15, 23, 42, .5); backdrop-filter: blur(5px); }
    .sr-card { width: min(980px, 100%); max-height: calc(100vh - 48px); display: flex; flex-direction: column;
      background: var(--surface, #fff); color: var(--text-primary, #0f172a); border-radius: 22px;
      box-shadow: 0 40px 100px rgba(15, 23, 42, .35); overflow: hidden; font-size: 14px; }
    .sr-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 26px 32px 18px;
      border-bottom: 1px solid var(--border, #e2e8f0); }
    .sr-eyebrow { font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--text-secondary, #64748b); }
    .sr-head h2 { margin: 4px 0 0; font-size: 26px; line-height: 1.2; }
    .sr-head-right { display: flex; align-items: center; gap: 12px; }
    .sr-chip { font-size: 12px; font-weight: 700; padding: 6px 14px; border-radius: 999px; background: #f1f5f9; color: #475569; }
    .sr-chip.pos { background: #dcfce7; color: #166534; }
    .sr-x { border: 0; background: #f1f5f9; width: 40px; height: 40px; border-radius: 12px; font-size: 26px; line-height: 1;
      cursor: pointer; color: var(--text-secondary, #64748b); }
    .sr-x:hover { background: #e2e8f0; }
    .sr-body { padding: 22px 32px 10px; overflow-y: auto; }
    .sr-lede { margin: 0 0 16px; font-size: 14px; color: var(--text-secondary, #475569); line-height: 1.6; }
    .sr-note { font-size: 14px; padding: 12px 16px; border-radius: 12px; margin-bottom: 16px; background: #f1f5f9; color: #334155; }
    .sr-note.warn { background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }
    .sr-note.err { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
    .sr-loading, .sr-empty { font-size: 14px; color: var(--text-secondary, #64748b); padding: 14px 4px; }
    .sr-empty { background: #f8fafc; border: 1px dashed var(--border-strong, #cbd5e1); border-radius: 12px; padding: 16px; margin-bottom: 8px; }

    .sr-preview { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 16px; margin-bottom: 18px;
      border-radius: 14px; background: #f8fafc; border: 1px solid var(--border, #e2e8f0); }
    .sr-preview-label { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--text-secondary, #64748b); margin-right: 4px; }
    .sr-pill { font-size: 13px; font-weight: 600; padding: 5px 12px; border-radius: 999px; background: #e2e8f0; color: #334155; }
    .sr-pill.pick { background: #dbeafe; color: #1e40af; }

    .sr-section { margin: 4px 0 28px; }
    .sr-section-head { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
    .sr-step { flex: none; width: 34px; height: 34px; border-radius: 50%; background: #0f172a; color: #fff; display: flex;
      align-items: center; justify-content: center; font-weight: 800; font-size: 15px; }
    .sr-section-text { flex: 1; min-width: 0; }
    .sr-section-text h3 { margin: 0; font-size: 18px; }
    .sr-section-text p { margin: 3px 0 0; font-size: 13px; color: var(--text-secondary, #64748b); }
    .sr-add { border: 1px solid #bfdbfe; background: #eff6ff; color: #1d4ed8; font-weight: 700; font-size: 14px; padding: 10px 16px;
      border-radius: 12px; cursor: pointer; white-space: nowrap; }
    .sr-add:hover { background: #dbeafe; }

    .sr-cols { display: grid; grid-template-columns: 1fr 150px 48px; gap: 12px; padding: 0 4px 6px; font-size: 12px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .05em; color: var(--text-secondary, #64748b); }
    .sr-cols.opt { grid-template-columns: 1fr 170px 130px; padding-left: 4px; }
    .sr-row, .sr-opt { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
    .sr-opt, .sr-row { flex-wrap: wrap; }
    .sr-picker { position: relative; flex: 1 1 280px; min-width: 220px; }
    .sr-input { width: 100%; height: 46px; padding: 0 14px; border: 1px solid var(--border-strong, #cbd5e1); border-radius: 12px; font: inherit; font-size: 15px;
      background: var(--surface, #fff); color: inherit; outline: none; box-sizing: border-box; }
    .sr-input:focus { border-color: #2563eb; box-shadow: 0 0 0 4px rgba(37, 99, 235, .15); }
    .sr-input:disabled { background: #f8fafc; color: #64748b; }
    .sr-input.num { width: 110px; text-align: right; }
    .sr-qty { display: flex; align-items: center; gap: 8px; flex: none; }
    .sr-unit-select { width: 92px; padding: 0 8px; }
    .sr-pack { display: flex; align-items: center; gap: 8px; flex: 1 0 100%; font-size: 13px; font-weight: 600; color: #92400e;
      background: #fffbeb; border: 1px solid #fcd34d; border-radius: 12px; padding: 8px 12px; }
    .sr-input.sm { height: 36px; width: 90px; font-size: 14px; }
    .sr-pack select.sr-input.sm { width: 76px; }
    .sr-unit { font-size: 14px; font-weight: 600; color: var(--text-secondary, #64748b); min-width: 34px; }
    .sr-del { flex: none; border: 0; background: #f1f5f9; color: #64748b; width: 40px; height: 40px; border-radius: 12px; font-size: 22px; line-height: 1; cursor: pointer; }
    .sr-del:hover { background: #fee2e2; color: #b91c1c; }

    .sr-menu { position: absolute; left: 0; right: 0; top: calc(100% + 6px); z-index: 20; max-height: 280px; overflow-y: auto;
      background: var(--surface, #fff); border: 1px solid var(--border, #e2e8f0); border-radius: 14px; box-shadow: 0 18px 44px rgba(15, 23, 42, .22); }
    .sr-menu-item { display: flex; flex-direction: column; align-items: flex-start; width: 100%; text-align: left; border: 0; background: transparent;
      padding: 10px 16px; cursor: pointer; font: inherit; color: inherit; }
    .sr-menu-item:hover { background: #f1f5f9; }
    .sr-menu-item strong { font-size: 14px; }
    .sr-menu-item span { font-size: 12px; color: var(--text-secondary, #64748b); }
    .sr-menu-empty { padding: 16px; font-size: 13px; color: var(--text-secondary, #64748b); }

    .sr-group { border: 1px solid var(--border, #e2e8f0); border-left: 6px solid #2563eb; border-radius: 16px; padding: 18px 20px; margin-bottom: 18px; background: #fcfdff; }
    .sr-group-head { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; flex-wrap: wrap; }
    .sr-group-tag { flex: none; font-size: 12px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; color: #1d4ed8; background: #dbeafe;
      padding: 6px 12px; border-radius: 999px; }
    .sr-group-name { flex: 1 1 260px; font-weight: 700; font-size: 16px; }
    .sr-remove { border: 0; background: transparent; color: #b91c1c; font-weight: 700; font-size: 13px; cursor: pointer; padding: 8px 6px; }
    .sr-remove:hover { text-decoration: underline; }

    .sr-switch { display: inline-flex; align-items: center; gap: 10px; cursor: pointer; white-space: nowrap; font-size: 14px; font-weight: 600; color: #334155; }
    .sr-switch input { position: absolute; opacity: 0; pointer-events: none; }
    .sr-track { width: 44px; height: 26px; border-radius: 999px; background: #cbd5e1; position: relative; transition: background .15s; }
    .sr-knob { position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(15, 23, 42, .3); transition: left .15s; }
    .sr-switch input:checked + .sr-track { background: #2563eb; }
    .sr-switch input:checked + .sr-track .sr-knob { left: 21px; }
    .sr-switch.disabled { cursor: default; opacity: .7; }

    .sr-mode { display: inline-flex; flex: none; background: #f1f5f9; border-radius: 12px; padding: 4px; }
    .sr-mode button { border: 0; background: transparent; padding: 9px 12px; border-radius: 9px; font-size: 13px; font-weight: 700; color: #64748b; cursor: pointer; white-space: nowrap; }
    .sr-mode button.on { background: #fff; color: #0f172a; box-shadow: 0 1px 4px rgba(15, 23, 42, .18); }
    .sr-default { flex: none; display: inline-flex; align-items: center; gap: 8px; padding: 10px 14px; border: 1px solid var(--border-strong, #cbd5e1);
      border-radius: 12px; font-size: 13px; font-weight: 700; color: #475569; cursor: pointer; white-space: nowrap; }
    .sr-default.on { background: #ecfdf5; border-color: #6ee7b7; color: #047857; }
    .sr-default.disabled { cursor: default; opacity: .7; }
    .sr-add-option { border: 1px dashed #93c5fd; background: #f5f9ff; color: #1d4ed8; font-weight: 700; font-size: 14px; padding: 11px 16px; border-radius: 12px; cursor: pointer; margin-top: 4px; }
    .sr-add-option:hover { background: #eaf2ff; }
    .sr-add-group { display: flex; flex-direction: column; align-items: center; gap: 2px; width: 100%; border: 2px dashed #94a3b8; background: #f8fafc; color: #0f172a;
      font-weight: 800; font-size: 16px; padding: 18px; border-radius: 16px; cursor: pointer; }
    .sr-add-group span { font-size: 12px; font-weight: 500; color: var(--text-secondary, #64748b); }
    .sr-add-group:hover { border-color: #2563eb; background: #eff6ff; }

    .sr-foot { display: flex; align-items: center; gap: 12px; padding: 18px 32px; border-top: 1px solid var(--border, #e2e8f0); background: var(--surface, #fff); }
    .sr-foot-note { font-size: 13px; font-weight: 600; color: var(--text-secondary, #64748b); }
    .sr-spacer { flex: 1; }
    .sr-btn { border: 0; border-radius: 12px; padding: 13px 26px; font-weight: 800; font-size: 15px; cursor: pointer; }
    .sr-btn.primary { background: #0f172a; color: #fff; }
    .sr-btn.primary:hover:not(:disabled) { background: #1e293b; }
    .sr-btn.ghost { background: #f1f5f9; color: #334155; }
    .sr-btn:disabled { opacity: .55; cursor: default; }

    @media (max-width: 760px) {
      .sr-overlay { padding: 8px; }
      .sr-head, .sr-body, .sr-foot { padding-left: 16px; padding-right: 16px; }
      .sr-head h2 { font-size: 21px; }
      .sr-cols { display: none; }
      .sr-row, .sr-opt { flex-wrap: wrap; }
      .sr-picker { flex-basis: 100%; }
      .sr-group { padding: 14px; }
    }
  `],
})
export class ServiceRecipeComponent implements OnInit {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);

  @Input() service: any;
  /** An all-branches role with Admin > Services > Edit; the server enforces it too. */
  @Input() canEdit = false;
  /** Emits true when the recipe was saved. */
  @Output() closed = new EventEmitter<boolean>();

  loading = true;
  saving = false;
  error = '';
  source = 'sap';
  fixed: FixedRow[] = [];
  groups: GroupRow[] = [];
  categories: string[] = [];

  pickerKey = '';
  results: any[] = [];
  searching = false;
  private searchTimer: any = null;
  private searchToken = 0;

  ngOnInit() {
    this.api.get(`services/api/master/${this.service.id}/recipe/`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (r: any) => {
        this.source = r.source;
        this.fixed = (r.fixed || []).map((f: any) => ({
          product_code: f.product_code, product_name: f.product_name, quantity: f.quantity, unit: f.unit || '',
          unit_options: f.unit_options || [], pack_size: '', pack_unit: 'ml' }));
        this.groups = (r.groups || []).map((g: any) => ({
          name: g.name, required: g.required,
          options: (g.options || []).map((o: any) => ({
            mode: o.by_category ? 'category' : 'product', product_code: o.product_code,
            product_name: o.product_name, category: o.by_category ? o.category : '',
            quantity: o.quantity, unit: o.unit || '', is_default: o.is_default,
            unit_options: o.unit_options || [], pack_size: '', pack_unit: 'ml' })),
        }));
        this.loading = false;
        this.cdr.markForCheck();
      },
      error: (e: any) => {
        this.loading = false;
        this.error = this.messageOf(e, 'The recipe could not be loaded.');
        this.cdr.markForCheck();
      },
    });
    this.api.get('services/api/master/recipe-products/', { q: '' }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (r: any) => { this.categories = r.categories || []; this.cdr.markForCheck(); },
      error: () => { /* the category list is optional */ },
    });
  }

  // ── Editing ────────────────────────────────────────────────────────────
  addFixed() { this.fixed.push({ product_code: '', product_name: '', quantity: '1', unit: '', unit_options: [], pack_size: '', pack_unit: 'ml' }); }

  addGroup() {
    // Two empty options to start with: a choice with one option is not a choice.
    this.groups.push({ name: '', required: true, options: [this.blankOption(), this.blankOption()] });
  }

  addOption(g: GroupRow) { g.options.push(this.blankOption()); }

  private blankOption(): OptionRow {
    return { mode: 'product', product_code: '', product_name: '', category: '', quantity: '1', unit: '', is_default: false,
      unit_options: [], pack_size: '', pack_unit: 'ml' };
  }

  setMode(o: OptionRow, mode: 'product' | 'category') {
    if (o.mode === mode) return;
    o.mode = mode;
    o.product_code = ''; o.product_name = ''; o.category = ''; o.unit = ''; o.unit_options = [];
  }

  toggleDefault(g: GroupRow, o: OptionRow) {
    const turnOn = !o.is_default;
    g.options.forEach(x => x.is_default = false);
    o.is_default = turnOn;
  }

  // ── Product search ─────────────────────────────────────────────────────
  search(key: string, term: string) {
    this.pickerKey = key;
    clearTimeout(this.searchTimer);
    const token = ++this.searchToken;
    this.searching = true;
    this.cdr.markForCheck();
    this.searchTimer = setTimeout(() => {
      this.api.get('services/api/master/recipe-products/', { q: term }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (r: any) => {
          if (token !== this.searchToken) return;
          this.results = r.products || [];
          this.searching = false;
          this.cdr.markForCheck();
        },
        error: () => { if (token === this.searchToken) { this.results = []; this.searching = false; this.cdr.markForCheck(); } },
      });
    }, term ? 220 : 0);
  }

  closePickerLater() {
    // Let a click on a result land first.
    setTimeout(() => { this.pickerKey = ''; this.cdr.markForCheck(); }, 160);
  }

  pickFixed(i: number, p: any) {
    this.fixed[i].product_code = p.product_code;
    this.fixed[i].product_name = p.name;
    this.fixed[i].unit = p.unit;
    this.fixed[i].unit_options = p.unit_options || [];
    this.fixed[i].pack_size = '';
    this.pickerKey = '';
  }

  pickOption(o: OptionRow, p: any) {
    o.product_code = p.product_code;
    o.product_name = p.name;
    o.unit = p.unit;
    o.unit_options = p.unit_options || [];
    o.pack_size = '';
    this.pickerKey = '';
  }

  // ── Units ──────────────────────────────────────────────────────────────
  /** Units a measure can be turned into when the product says what one unit holds. */
  readonly packUnits = ['ml', 'l', 'g', 'kg'];

  /** The product's own unit comes first in the options the server sends. */
  stockUnit(r: UnitFields): string { return r.unit_options[0] || 'pcs'; }

  /** Its own units, plus the usual measures (the server asks for what one unit holds when needed). */
  unitChoices(r: UnitFields & { unit: string }): string[] {
    const all = [...r.unit_options];
    for (const u of this.packUnits) if (!all.includes(u)) all.push(u);
    if (r.unit && !all.includes(r.unit)) all.push(r.unit);
    return all;
  }

  /** A measure was picked that this product cannot be turned into yet. */
  needsPack(r: UnitFields & { unit: string }): boolean {
    return !!r.unit && r.unit_options.length > 0 && !r.unit_options.includes(r.unit);
  }

  /**
   * A measure was picked for a product SAP holds in pieces. SAP has no ml/g for it, so the pack size has
   * to come from somewhere: many product names say it ("SERUM 30ML", "MASK 500ML"), so offer that
   * (the person can still change it); otherwise the box stays empty and is asked for.
   */
  onUnitPicked(r: (FixedRow | OptionRow) & { unit: string }) {
    if (!this.needsPack(r) || Number(r.pack_size) > 0) return;
    const name = (r as FixedRow).product_name || '';
    const m = /(\d+(?:[.,]\d+)?)\s*(ml|ltr|l|gm|g|kg)\b/i.exec(name);
    if (!m) return;
    const unit = ({ ltr: 'l', gm: 'g' } as any)[m[2].toLowerCase()] || m[2].toLowerCase();
    r.pack_size = m[1].replace(',', '.');
    r.pack_unit = unit;
  }

  private unitPayload(r: UnitFields & { unit: string }) {
    const out: any = { unit: r.unit && r.unit !== this.stockUnit(r) ? r.unit : '' };
    if (this.needsPack(r)) { out.pack_size = String(r.pack_size); out.pack_unit = r.pack_unit; }
    return out;
  }

  // ── Save ───────────────────────────────────────────────────────────────
  private problem(): string {
    for (const f of this.fixed) {
      if (!f.product_code) return 'Pick a product for every "Always used" row, or remove the row.';
      if (!(Number(f.quantity) > 0)) return `${f.product_name || f.product_code}: the quantity must be more than zero.`;
      if (this.needsPack(f) && !(Number(f.pack_size) > 0)) return `${f.product_name}: SAP keeps it in ${this.stockUnit(f)}. To use it in ${f.unit}, type how many ${f.pack_unit} one ${this.stockUnit(f)} holds in the yellow box under the row - or set the unit back to ${this.stockUnit(f)}.`;
    }
    const names = new Set<string>();
    for (const g of this.groups) {
      const name = (g.name || '').trim();
      if (!name) return 'Give every choice a name, for example Colour.';
      if (names.has(name.toLowerCase())) return `There are two choices called ${name}.`;
      names.add(name.toLowerCase());
      if (!g.options.length) return `${name}: add at least one option.`;
      for (const o of g.options) {
        if (o.mode === 'product' && !o.product_code) return `${name}: pick a product for every option, or remove it.`;
        if (o.mode === 'category' && !o.category) return `${name}: choose a category for the category option.`;
        if (!(Number(o.quantity) > 0)) return `${name}: every option needs a quantity more than zero.`;
        if (o.mode === 'product' && this.needsPack(o) && !(Number(o.pack_size) > 0)) return `${name}: SAP keeps ${o.product_name} in ${this.stockUnit(o)}. To use it in ${o.unit}, type how many ${o.pack_unit} one ${this.stockUnit(o)} holds in the yellow box under the row - or set the unit back to ${this.stockUnit(o)}.`;
      }
    }
    return '';
  }

  save() {
    this.error = this.problem();
    if (this.error) { this.cdr.markForCheck(); return; }
    this.saving = true;
    const payload = {
      fixed: this.fixed.map(f => ({ product_code: f.product_code, quantity: String(f.quantity), ...this.unitPayload(f) })),
      groups: this.groups.map(g => ({
        name: g.name.trim(), required: g.required,
        options: g.options.map(o => ({
          product_code: o.mode === 'product' ? o.product_code : '',
          category: o.mode === 'category' ? o.category : '',
          quantity: String(o.quantity), is_default: o.is_default,
          ...(o.mode === 'product' ? this.unitPayload(o) : {}) })),
      })),
    };
    this.api.put(`services/api/master/${this.service.id}/recipe/`, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => { this.saving = false; this.closed.emit(true); },
      error: (e: any) => {
        this.saving = false;
        this.error = this.messageOf(e, 'The recipe could not be saved.');
        this.cdr.markForCheck();
      },
    });
  }

  private messageOf(e: any, fallback: string): string {
    const body = e?.error || {};
    const pick = (v: any) => Array.isArray(v) ? v[0] : v;
    return pick(body.recipe) || pick(body.detail) || fallback;
  }

  close(saved: boolean) { this.closed.emit(saved); }

  onOverlay(event: MouseEvent) {
    if ((event.target as HTMLElement).classList.contains('sr-overlay')) this.close(false);
  }

  trackByIndex(index: number) { return index; }
  trackByCode(index: number, p: any) { return p.product_code; }
}
