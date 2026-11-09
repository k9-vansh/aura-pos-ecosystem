import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, inject, OnInit, OnDestroy, DestroyRef, ChangeDetectionStrategy, ChangeDetectorRef, ElementRef, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { AuthService } from '../services/auth';
import { SoundService } from '../services/sound.service';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-login',
  imports: [FormsModule, CommonModule],
  templateUrl: './login.html',
  styleUrl: './login.css',
})
export class LoginComponent implements OnInit, OnDestroy {
  private destroyRef = inject(DestroyRef);
  private cdr = inject(ChangeDetectorRef);
  authService = inject(AuthService);
  private sound = inject(SoundService);

  @ViewChild('tilt', { static: true }) tilt?: ElementRef<HTMLElement>;
  @ViewChild('email', { static: true }) email?: ElementRef<HTMLInputElement>;

  credentials = {
    username: '',
    password: ''
  };

  error = '';
  isLoading = false;
  signedIn = false;
  showPassword = false;
  capsLock = false;
  /** True while the receipt is feeding out of the printer (drives the status LED). */
  printing = true;
  /** Bumped on every failed attempt; its parity swaps the shake class so the animation replays. */
  shakeTick = 0;
  /** Ticks every second so the receipt header shows a live time. */
  now = new Date();

  static hasLoaded = false;
  isInitialLoad = !LoginComponent.hasLoaded;

  private frame = 0;
  private clock?: ReturnType<typeof setInterval>;
  private timers: ReturnType<typeof setTimeout>[] = [];
  private cancelWait: () => void = () => undefined;

  ngOnInit() {
    LoginComponent.hasLoaded = true;
    // With reduced motion the paper never animates, so there is no "finished printing" event to wait for.
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) this.printing = false;
    this.clock = setInterval(() => {
      this.now = new Date();
      this.cdr.markForCheck();
    }, 1000);

    // The printer: one run of the paper-feed sound, in the same four bursts as the CSS
    // `feed` (it starts 0.7s after the printer appears and runs 3.6s). If the browser is
    // still keeping the page silent (nobody has touched it yet) the sound waits for the
    // first click or key press and joins in where the paper has got to, so it is not
    // "sometimes there, sometimes not".
    const feedStart = Date.now() + (this.isInitialLoad ? 3350 : 50) + 700;
    const bursts: Array<[number, number]> = [[0, 0.612], [0.828, 1.512], [1.692, 2.448], [2.592, 3.312]];
    this.cancelWait = this.sound.whenRunning(() => {
      if (this.isLoading || this.signedIn) return;
      const wait = Math.max(0, feedStart - Date.now());
      const elapsed = Math.max(0, (Date.now() - feedStart) / 1000);
      const rest = bursts
        .filter(([, end]) => end > elapsed + 0.05)
        .map(([from, end]): [number, number] => [Math.max(0, from - elapsed), end - elapsed]);
      if (!rest.length) return;
      this.timers.push(setTimeout(() => {
        if (!this.isLoading && !this.signedIn) this.sound.playPrinter(rest);
      }, wait));
    });
  }

  ngOnDestroy() {
    if (this.clock) clearInterval(this.clock);
    this.cancelWait();
    this.timers.forEach(clearTimeout);
    this.sound.stopPrinter();
    if (this.frame) cancelAnimationFrame(this.frame);
  }

  /** The receipt has finished feeding out: stop the printer light and put the cursor in the email field. */
  onPrinted(event: AnimationEvent) {
    if (event.animationName !== 'feed') return;
    this.printing = false;
    this.cdr.markForCheck();
    const input = this.email?.nativeElement;
    if (input && !document.activeElement?.closest('input')) input.focus({ preventScroll: true });
  }

  /** Tilts the printer a touch toward the cursor (style only, no change detection). */
  onMouseMove(event: MouseEvent) {
    if (this.frame) return;
    const x = event.clientX / window.innerWidth - 0.5;
    const y = event.clientY / window.innerHeight - 0.5;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      const el = this.tilt?.nativeElement;
      if (!el) return;
      el.style.setProperty('--rx', `${(-y * 2.6).toFixed(2)}deg`);
      el.style.setProperty('--ry', `${(x * 3.4).toFixed(2)}deg`);
    });
  }

  onKey(event: KeyboardEvent) {
    const on = event.getModifierState?.('CapsLock') ?? false;
    if (on !== this.capsLock) {
      this.capsLock = on;
      this.cdr.markForCheck();
    }
  }

  private fail(message: string) {
    this.error = message;
    this.shakeTick++;
  }

  onSubmit() {
    this.error = '';
    if (!this.credentials.username.trim() || !this.credentials.password) {
      this.fail('Enter your email and password');
      return;
    }
    this.isLoading = true;
    this.cancelWait();
    this.timers.forEach(clearTimeout);
    this.sound.stopPrinter();
    this.authService.login(this.credentials).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isLoading = false;
        this.signedIn = true;
        this.sound.play('success');
        this.cdr.markForCheck();
      },
      error: (err) => {
        this.isLoading = false;
        if (err.status === 400 || err.status === 401) {
          this.fail('Invalid email or password');
        } else {
          this.fail(`Network Error (${err.status}): Could not connect to backend`);
        }
        this.cdr.markForCheck();
      }
    });
  }
}
