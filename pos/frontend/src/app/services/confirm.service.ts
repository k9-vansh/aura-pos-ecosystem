import { Injectable, signal } from '@angular/core';

export interface ConfirmRequest {
  message: string;
  /** Present when the dialog asks for a line of text instead of a yes/no. */
  input?: { placeholder: string; value: string };
  destructive: boolean;
  confirmLabel: string;
}

/**
 * In-app replacement for window.confirm / window.prompt, so questions look like
 * the rest of the POS instead of the browser's "localhost says" box.
 */
@Injectable({ providedIn: 'root' })
export class ConfirmService {
  request = signal<ConfirmRequest | null>(null);
  private resolver: ((value: any) => void) | null = null;

  /** Resolves true when the user confirms, false on Cancel / Esc / backdrop. */
  ask(message: string, options: { confirmLabel?: string; destructive?: boolean } = {}): Promise<boolean> {
    const destructive = options.destructive ?? /delete|deactivate|discard|remove|cancel|lock|archive|merge/i.test(message);
    return this.open({
      message, destructive,
      confirmLabel: options.confirmLabel ?? (destructive ? 'Yes, continue' : 'Confirm'),
    }).then(v => v === true);
  }

  /** Resolves the trimmed text, or null when cancelled. */
  askText(message: string, placeholder = ''): Promise<string | null> {
    return this.open({ message, input: { placeholder, value: '' }, destructive: false, confirmLabel: 'Save' })
      .then(v => (typeof v === 'string' ? v : null));
  }

  answer(value: boolean | string | null) {
    const resolve = this.resolver;
    this.resolver = null;
    this.request.set(null);
    resolve?.(value);
  }

  private open(req: ConfirmRequest): Promise<any> {
    // A second question while one is open cancels the first.
    this.resolver?.(false);
    this.request.set(req);
    return new Promise(resolve => { this.resolver = resolve; });
  }
}
