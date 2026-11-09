import { Injectable } from '@angular/core';

export type SoundName =
  | 'click' | 'tab' | 'toggle' | 'open' | 'close'
  | 'success' | 'error' | 'info' | 'warning' | 'scan' | 'cash' | 'print' | 'send' | 'receive';

const STORAGE_KEY = 'pos.sound';

/** One note of a sound: when it starts (s), pitch (Hz), length (s), peak gain, wave shape. */
interface Note { at: number; freq: number; len: number; gain: number; type?: OscillatorType; slideTo?: number }

/**
 * Sound effects, synthesised with the Web Audio API: there are no audio files to
 * download, nothing to decode and nothing to fail to load. Every sound is a few
 * short notes with a quick attack and release so it never clicks or pops.
 *
 * The browser only lets a page make sound after the user has touched it, so the
 * AudioContext is created on the first gesture. Until then play() does nothing.
 */
@Injectable({ providedIn: 'root' })
export class SoundService {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private lastAt = 0;
  private on = SoundService.readPreference();

  private static readPreference(): boolean {
    try { return localStorage.getItem(STORAGE_KEY) !== '0'; } catch { return true; }
  }

  get enabled(): boolean { return this.on; }

  setEnabled(value: boolean) {
    this.on = value;
    try { localStorage.setItem(STORAGE_KEY, value ? '1' : '0'); } catch { /* private mode */ }
    if (value) this.play('toggle', true);
  }

  toggle(): boolean {
    this.setEnabled(!this.on);
    return this.on;
  }

  private waiting: Array<() => void> = [];

  /** Call from a user gesture: creates / resumes the audio engine. */
  unlock() {
    const ctx = this.engine();
    if (ctx && ctx.state === 'suspended') ctx.resume().catch(() => undefined);
  }

  /**
   * Runs `fn` as soon as sound is allowed. A browser keeps a page silent until the
   * visitor has touched it, so a sound due on a freshly opened page waits for the
   * first click or key press instead of being lost. Returns a function that
   * cancels the wait.
   */
  whenRunning(fn: () => void): () => void {
    const ctx = this.engine();
    if (ctx && ctx.state === 'running') { fn(); return () => undefined; }
    this.waiting.push(fn);
    return () => { this.waiting = this.waiting.filter(f => f !== fn); };
  }

  private flushWaiting() {
    const todo = this.waiting;
    this.waiting = [];
    for (const fn of todo) { try { fn(); } catch { /* a sound must never break the page */ } }
  }

  play(name: SoundName, force = false) {
    if (!this.on && !force) return;
    const ctx = this.engine();
    if (!ctx || !this.master || ctx.state === 'closed') return;
    if (ctx.state === 'suspended') { ctx.resume().catch(() => undefined); }

    // A burst of identical events (a held key, a fast double click) stays a single sound.
    const now = performance.now();
    if (name === 'click' && now - this.lastAt < 35) return;
    this.lastAt = now;

    if (name === 'print') { this.playPrinter([[0, 1.1]]); return; }

    const t0 = ctx.currentTime + 0.005;
    for (const n of SOUNDS[name]) this.note(ctx, t0, n);
  }

  // ---------- the printer ----------

  private printBus: GainNode | null = null;
  private printSources: AudioScheduledSourceNode[] = [];
  private noise: AudioBuffer | null = null;

  /**
   * A receipt printer at work: for every burst [start, end] (seconds from now) a
   * motor whine, a stepper "tick-tick-tick" and the buzz of the print head, ending
   * with the cutter click. Everything runs on one bus so stopPrinter() can cut it
   * off at once.
   */
  playPrinter(bursts: Array<[number, number]>) {
    if (!this.on) return;
    const ctx = this.engine();
    const master = this.master;
    if (!ctx || !master || ctx.state === 'closed') return;
    if (ctx.state === 'suspended') ctx.resume().catch(() => undefined);
    this.stopPrinter();

    const bus = ctx.createGain();
    bus.gain.value = 1;
    bus.connect(master);
    this.printBus = bus;
    const sources = this.printSources;
    const t0 = ctx.currentTime + 0.03;
    const noise = this.noiseBuffer(ctx);

    /** A short click of filtered noise: one step of the paper motor. */
    const tick = (at: number, freq: number, gain: number, len = 0.024, q = 1.4) => {
      const src = ctx.createBufferSource();
      src.buffer = noise;
      const filter = ctx.createBiquadFilter();
      filter.type = 'bandpass';
      filter.frequency.value = freq;
      filter.Q.value = q;
      const amp = ctx.createGain();
      amp.gain.setValueAtTime(0.0001, t0 + at);
      amp.gain.exponentialRampToValueAtTime(gain, t0 + at + 0.003);
      amp.gain.exponentialRampToValueAtTime(0.0001, t0 + at + len);
      src.connect(filter); filter.connect(amp); amp.connect(bus);
      src.start(t0 + at, Math.random() * 0.4);
      src.stop(t0 + at + len + 0.02);
      sources.push(src);
    };

    /** The low "tock" of the roller turning one notch. */
    const thump = (at: number, freq: number, gain: number) => {
      const osc = ctx.createOscillator();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, t0 + at);
      osc.frequency.exponentialRampToValueAtTime(freq * 0.6, t0 + at + 0.012);
      const amp = ctx.createGain();
      amp.gain.setValueAtTime(0.0001, t0 + at);
      amp.gain.exponentialRampToValueAtTime(gain, t0 + at + 0.002);
      amp.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.014);
      osc.connect(amp); amp.connect(bus);
      osc.start(t0 + at);
      osc.stop(t0 + at + 0.02);
      sources.push(osc);
    };

    for (const [start, end] of bursts) {
      const len = Math.max(0.1, end - start);

      // The soft hiss of paper sliding under the thermal head. Noise only: no pitched
      // motor tone, which is what made the first version sound like a power tool.
      const rub = ctx.createBufferSource();
      rub.buffer = noise;
      rub.loop = true;
      const rubBand = ctx.createBiquadFilter();
      rubBand.type = 'bandpass';
      rubBand.frequency.value = 2600;
      rubBand.Q.value = 0.55;
      const rubAmp = ctx.createGain();
      rubAmp.gain.setValueAtTime(0.0001, t0 + start);
      rubAmp.gain.exponentialRampToValueAtTime(0.028, t0 + start + 0.04);
      rubAmp.gain.setValueAtTime(0.028, t0 + end - 0.04);
      rubAmp.gain.exponentialRampToValueAtTime(0.0001, t0 + end);
      rub.connect(rubBand); rubBand.connect(rubAmp); rubAmp.connect(bus);
      rub.start(t0 + start);
      rub.stop(t0 + end + 0.03);
      sources.push(rub);

      // The stepper: one small "tk" per line of paper, about 35 a second, each a
      // little different in pitch, strength and timing so it is not a machine-gun.
      tick(start, 180, 0.35, 0.05, 0.8); // the roller engaging
      for (let t = 0.03, i = 0; t < len - 0.015; t += 0.0285, i++) {
        const jitter = (Math.random() - 0.5) * 0.003;
        const strength = 0.13 + Math.random() * 0.05;
        tick(start + t + jitter, i % 2 ? 1150 : 1550, strength, 0.012, 1.0);
        thump(start + t + jitter, 135, 0.05);
      }
    }

    // The cutter click when the paper stops.
    const last = bursts[bursts.length - 1];
    if (last) {
      tick(last[1] + 0.03, 3600, 0.22, 0.018, 0.9); // the tear bar
      tick(last[1] + 0.05, 260, 0.28, 0.04, 0.8);
    }
  }

  /** Cut the printer sound off immediately (leaving the screen, signing in). */
  stopPrinter() {
    const ctx = this.ctx;
    if (!ctx) return;
    const bus = this.printBus;
    if (bus) {
      try {
        bus.gain.cancelScheduledValues(ctx.currentTime);
        bus.gain.setTargetAtTime(0, ctx.currentTime, 0.015);
      } catch { /* closed */ }
      setTimeout(() => { try { bus.disconnect(); } catch { /* already gone */ } }, 150);
    }
    for (const src of this.printSources) { try { src.stop(ctx.currentTime + 0.06); } catch { /* not started / done */ } }
    this.printSources = [];
    this.printBus = null;
  }

  private noiseBuffer(ctx: AudioContext): AudioBuffer {
    if (this.noise && this.noise.sampleRate === ctx.sampleRate) return this.noise;
    const buf = ctx.createBuffer(1, Math.floor(ctx.sampleRate * 0.6), ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    this.noise = buf;
    return buf;
  }

  private engine(): AudioContext | null {
    if (this.ctx) return this.ctx;
    const Ctor = window.AudioContext ?? (window as any).webkitAudioContext;
    if (!Ctor) return null;
    try {
      this.ctx = new Ctor();
      this.ctx!.onstatechange = () => { if (this.ctx?.state === 'running') this.flushWaiting(); };
      this.master = this.ctx!.createGain();
      this.master.gain.value = 0.55;
      // A gentle compressor keeps stacked sounds from clipping.
      const comp = this.ctx!.createDynamicsCompressor();
      this.master.connect(comp);
      comp.connect(this.ctx!.destination);
    } catch {
      this.ctx = null;
    }
    return this.ctx;
  }

  private note(ctx: AudioContext, t0: number, n: Note) {
    const osc = ctx.createOscillator();
    const amp = ctx.createGain();
    const start = t0 + n.at;
    const end = start + n.len;
    osc.type = n.type ?? 'sine';
    osc.frequency.setValueAtTime(n.freq, start);
    if (n.slideTo) osc.frequency.exponentialRampToValueAtTime(n.slideTo, end);
    amp.gain.setValueAtTime(0.0001, start);
    amp.gain.exponentialRampToValueAtTime(n.gain, start + 0.008);
    amp.gain.exponentialRampToValueAtTime(0.0001, end);
    osc.connect(amp);
    amp.connect(this.master!);
    osc.start(start);
    osc.stop(end + 0.02);
  }
}

const SOUNDS: Record<SoundName, Note[]> = {
  // a soft key tick
  click:   [{ at: 0, freq: 1250, len: 0.045, gain: 0.16, type: 'triangle', slideTo: 900 }],
  // tab / menu change: slightly higher and shorter
  tab:     [{ at: 0, freq: 1650, len: 0.05, gain: 0.15, type: 'triangle', slideTo: 1250 }],
  // checkbox / switch
  toggle:  [{ at: 0, freq: 700, len: 0.05, gain: 0.14, type: 'triangle' }, { at: 0.05, freq: 1050, len: 0.06, gain: 0.14, type: 'triangle' }],
  // a panel or dialog opens / closes
  open:    [{ at: 0, freq: 520, len: 0.09, gain: 0.13, type: 'sine', slideTo: 880 }],
  close:   [{ at: 0, freq: 820, len: 0.09, gain: 0.12, type: 'sine', slideTo: 480 }],
  // saved, paid, done: a bright rising triad
  success: [
    { at: 0.00, freq: 659.25, len: 0.14, gain: 0.2, type: 'sine' },
    { at: 0.09, freq: 830.61, len: 0.14, gain: 0.2, type: 'sine' },
    { at: 0.18, freq: 987.77, len: 0.26, gain: 0.22, type: 'sine' },
  ],
  // failed: two low falling buzzes
  error:   [
    { at: 0.00, freq: 240, len: 0.13, gain: 0.2, type: 'sawtooth', slideTo: 170 },
    { at: 0.15, freq: 200, len: 0.2, gain: 0.2, type: 'sawtooth', slideTo: 130 },
  ],
  warning: [{ at: 0, freq: 440, len: 0.12, gain: 0.17, type: 'square' }, { at: 0.14, freq: 440, len: 0.12, gain: 0.17, type: 'square' }],
  info:    [{ at: 0, freq: 880, len: 0.1, gain: 0.15, type: 'sine' }, { at: 0.09, freq: 1175, len: 0.16, gain: 0.15, type: 'sine' }],
  // barcode scanned: the classic till beep
  scan:    [{ at: 0, freq: 2100, len: 0.09, gain: 0.2, type: 'square' }],
  // money received: coin ring
  cash:    [
    { at: 0.00, freq: 1568, len: 0.1, gain: 0.17, type: 'triangle' },
    { at: 0.07, freq: 2093, len: 0.28, gain: 0.17, type: 'triangle' },
  ],
  // chat: a message leaves (soft upward swoosh) and one arrives (two-note ding)
  send:    [{ at: 0, freq: 520, len: 0.11, gain: 0.14, type: 'sine', slideTo: 1040 }],
  receive: [{ at: 0, freq: 880, len: 0.12, gain: 0.16, type: 'sine' }, { at: 0.1, freq: 1320, len: 0.2, gain: 0.15, type: 'sine' }],
  // receipt printing is a machine sound: see SoundService.playPrinter()
  print:   [],
};
