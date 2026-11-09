import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { finalize } from 'rxjs/operators';
import { LoadingService } from './loading.service';

export const loadingInterceptor: HttpInterceptorFn = (req, next) => {
  const loadingService = inject(LoadingService);
  
  if (!req.headers.has('X-Background-Request')) {
    loadingService.show();
  }
  
  return next(req).pipe(
    finalize(() => {
      if (!req.headers.has('X-Background-Request')) {
        loadingService.hide();
      }
    })
  );
};
