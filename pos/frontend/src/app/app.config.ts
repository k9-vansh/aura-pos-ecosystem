import { ApplicationConfig, LOCALE_ID, provideBrowserGlobalErrorListeners, isDevMode } from '@angular/core';
import { registerLocaleData } from '@angular/common';
import localeEnIn from '@angular/common/locales/en-IN';
import { provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { centerScopeInterceptor } from './services/center-scope';
import { authInterceptor } from './auth.interceptor';
import { masterCacheInterceptor } from './services/master-cache.interceptor';
import { preventDoubleSubmitInterceptor } from './prevent-double-submit.interceptor';
import { errorInterceptor } from './error.interceptor';
import { viewRefreshInterceptor } from './services/view-refresh';
import { loadingInterceptor } from './loading.interceptor';
import { routes } from './app.routes';
import { provideServiceWorker } from '@angular/service-worker';

// Indian digit grouping (12,34,567.00) and the rupee sign in every number and currency pipe.
registerLocaleData(localeEnIn);

export const appConfig: ApplicationConfig = {
  providers: [
    { provide: LOCALE_ID, useValue: 'en-IN' },
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes),
    provideHttpClient(withInterceptors([viewRefreshInterceptor, loadingInterceptor, centerScopeInterceptor, authInterceptor, masterCacheInterceptor, preventDoubleSubmitInterceptor, errorInterceptor])), provideServiceWorker('ngsw-worker.js', {
            enabled: !isDevMode(),
            registrationStrategy: 'registerWhenStable:30000'
          })
  ]
};
