import { Injectable, signal, Signal } from '@angular/core';

export interface ThemeOption {
  id: string;
  label: string;
  className: string;
  colorLeft: string;
  colorRight: string;
  scheme: 'dark' | 'light';
}

const STORAGE_KEY = 'tn-theme';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  readonly themes: ThemeOption[] = [
    { id: 'northern-ops', label: 'Northern Ops',  className: 'theme-northern-ops', colorLeft: '#071629', colorRight: '#1FB6A6', scheme: 'dark' },
    { id: 'maple-steel',  label: 'Maple & Steel', className: 'theme-maple-steel',  colorLeft: '#0A0D12', colorRight: '#E03131', scheme: 'dark' },
    { id: 'aurora-soc',   label: 'Aurora SOC',    className: 'theme-aurora-soc',   colorLeft: '#05070E', colorRight: '#2DE2C5', scheme: 'dark' },
    { id: 'great-white-north', label: 'Great White North', className: 'theme-great-white-north', colorLeft: '#F7F8FA', colorRight: '#D52B1E', scheme: 'light' },
  ];

  private readonly activeThemeSignal = signal<string>('northern-ops');
  readonly activeTheme: Signal<string> = this.activeThemeSignal.asReadonly();

  constructor() {
    const saved = localStorage.getItem(STORAGE_KEY) || 'northern-ops';
    this.applyTheme(saved);
  }

  setTheme(id: string): void {
    this.applyTheme(id);
    localStorage.setItem(STORAGE_KEY, id);
  }

  /** Resolved value of a CSS custom property on <body> (e.g. '--accent'). */
  cssVar(name: string): string {
    return getComputedStyle(document.body).getPropertyValue(name).trim();
  }

  accentColor(): string {
    return this.cssVar('--accent');
  }

  private applyTheme(id: string): void {
    const body = document.body;
    this.themes.forEach((t) => body.classList.remove(t.className));
    const found = this.themes.find((t) => t.id === id) ?? this.themes[0];
    body.classList.add(found.className);
    // Keep UA-rendered widgets (scrollbars, form controls) in step.
    document.documentElement.style.colorScheme = found.scheme;
    this.activeThemeSignal.set(found.id);
  }
}
