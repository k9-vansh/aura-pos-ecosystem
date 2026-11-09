import { ChangeDetectorRef, ComponentRef, Injectable } from '@angular/core';
import { HttpInterceptorFn, HttpResponse } from '@angular/common/http';
import { tap } from 'rxjs/operators';
import { inject } from '@angular/core';

/**
 * Keeps the open page in step with what the server just answered.
 *
 * The pages are OnPush and the app runs without zone.js, so a page whose request finished
 * (or failed) was redrawn only if its own handler remembered to ask. Many forgot on the
 * error path, and the screen kept showing "Saving..." or an old list until the next click.
 * After every request, whatever the answer, the open page is marked for a redraw; the
 * handlers have already changed their fields by the time that redraw runs.
 */
@Injectable({ providedIn: 'root' })
export class ViewRefresh {
  private page: ComponentRef<unknown> | null = null;

  /** The routed page, set by the layout's <router-outlet (activate)>. */
  attachRef(ref: ComponentRef<unknown> | null): void {
    this.page = ref;
  }

  poke(): void {
    try {
      // The page's OWN view: ComponentRef.changeDetectorRef is its host view, and marking that
      // would leave the OnPush component itself untouched.
      this.page?.injector.get(ChangeDetectorRef).markForCheck();
    } catch {
      this.page = null;            // the page was destroyed in the meantime
    }
  }
}

export const viewRefreshInterceptor: HttpInterceptorFn = (req, next) => {
  const refresh = inject(ViewRefresh);
  return next(req).pipe(tap({
    next: ev => { if (ev instanceof HttpResponse) refresh.poke(); },
    error: () => refresh.poke(),
  }));
};
