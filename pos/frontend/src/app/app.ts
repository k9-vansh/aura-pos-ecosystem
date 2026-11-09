import { Component, signal, inject, ChangeDetectionStrategy } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { ToastComponent } from './components/toast/toast';
import { CommonModule } from '@angular/common';
import { LoadingService } from './loading.service';
import { ConfirmComponent } from './components/confirm/confirm';
import { ToastService } from './services/toast.service';
import { startDarkGuard } from './dark-guard/dark-guard';
import { startSelectMenus } from './select-menu/select-menu';
import { startBoxFix } from './box-fix/box-fix';
import { todayLocal } from './services/date-util';
import { SoundService } from './services/sound.service';
import { startSoundEffects } from './services/sound-effects';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-root',
  imports: [RouterOutlet, ToastComponent, ConfirmComponent, CommonModule],
  templateUrl: './app.html',
  styleUrl: './app.css'
})
export class App {
  todayDate: string = todayLocal();
  protected readonly title = signal('pos-frontend');
  public loadingService = inject(LoadingService);
  private toasts = inject(ToastService);
  private sound = inject(SoundService);
  soundOn = signal(this.sound.enabled);

  toggleSound() {
    this.soundOn.set(this.sound.toggle());
  }

  constructor() {
    startSoundEffects(this.sound);
    // Every window.alert("...") in the app shows as an in-app toast instead of
    // the browser's "localhost says" box.
    window.alert = (message?: any) => this.toasts.notify(String(message ?? ''));
    startDarkGuard();
    startSelectMenus();
    startBoxFix();
  }
}
