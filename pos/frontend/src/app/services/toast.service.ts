import { Injectable, inject, signal } from '@angular/core';
import { SoundService } from './sound.service';

export type ToastType = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  message: string;
  type: ToastType;
}

@Injectable({
  providedIn: 'root'
})
export class ToastService {
  toasts = signal<Toast[]>([]);
  private idCounter = 0;
  private sound = inject(SoundService);

  /**
   * "Failed to create product: {"gst_percent":["Must be 0 to 100."]}" is what the screens build
   * from a server refusal. Nobody should read braces and quotes, so a trailing JSON part is
   * turned into plain words: "Failed to create product: Gst percent: Must be 0 to 100."
   */
  static plain(message: string): string {
    const text = String(message ?? '');
    const at = text.search(/[\[{]/);
    if (at < 0) return text;
    let parsed: unknown;
    try { parsed = JSON.parse(text.slice(at)); } catch { return text; }
    const words = (v: unknown, label = ''): string[] => {
      if (v == null) return [];
      if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
        const nice = label.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
        return [label && !/^\d+$/.test(label) && label !== 'detail' && label !== 'error' && label !== 'non_field_errors'
          ? `${nice}: ${v}` : String(v)];
      }
      if (Array.isArray(v)) return v.flatMap(x => words(x, label));
      return Object.entries(v as Record<string, unknown>).flatMap(([k, x]) => words(x, k));
    };
    const plain = words(parsed).join(' | ');
    if (!plain) return text.slice(0, at).replace(/[:\s]+$/, '');
    const head = text.slice(0, at).trim();
    return head ? `${head} ${plain}` : plain;
  }

  /** The wording of a refusal without its "Failed to create x:" lead-in, lower-cased. */
  private static core(message: string): string {
    return message.toLowerCase().replace(/^[^:]*:\s*/, '').trim();
  }

  show(message: string, type: ToastType = 'info', duration: number = 4000) {
    message = ToastService.plain(message);
    // One refusal used to show twice: once from the shared error handler and once from the
    // screen's own handler, worded alike. The same words are shown once.
    if (type === 'error') {
      const mine = ToastService.core(message);
      const repeated = mine.length >= 8 && this.toasts().some(t => {
        const theirs = ToastService.core(t.message);
        return t.type === 'error' && theirs.length >= 8 && (theirs.includes(mine) || mine.includes(theirs));
      });
      if (repeated) return;
    }
    this.sound.play(type === 'success' ? 'success' : type === 'error' ? 'error' : 'info');
    const id = this.idCounter++;
    this.toasts.update(current => [...current, { id, message, type }]);

    // Auto dismiss
    setTimeout(() => {
      this.remove(id);
    }, duration);
  }

  /**
   * Replacement for window.alert: the type is read from the wording, so the
   * existing alert("...") calls get a green tick, a red cross or a blue info
   * toast without each one being rewritten.
   */
  notify(message: string) {
    const text = String(message ?? '');
    const failed = /fail|error|cannot|can't|unable|invalid|not allowed|denied|refused|required|must |please |select |enter |already|no [a-z]+ (found|available)/i.test(text);
    const done = /success|saved|created|updated|deleted|recorded|submitted|sent|added|removed|locked|paid|activated|deactivated|imported/i.test(text);
    if (failed) this.showError(text);
    else if (done) this.showSuccess(text);
    else this.show(text, 'info', 5000);
  }

  showError(message: string) {
    this.show(message, 'error', 5000); // Errors stay slightly longer
  }

  showSuccess(message: string) {
    this.show(message, 'success');
  }

  remove(id: number) {
    this.toasts.update(current => current.filter(t => t.id !== id));
  }
}
