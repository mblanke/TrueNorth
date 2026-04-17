import { Component, Renderer2 } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule, RouterOutlet } from '@angular/router';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';

interface NavItem {
  label: string;
  icon: string;
  route: string;
}

interface NavSection {
  name: string;
  items: NavItem[];
}

interface ThemeOption {
  id: string;
  label: string;
  className: string;
  colorLeft: string;
  colorRight: string;
}

@Component({
  selector: 'tn-root',
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    RouterOutlet,
    MatSidenavModule,
    MatToolbarModule,
    MatListModule,
    MatIconModule,
    MatButtonModule,
    MatTooltipModule,
    MatExpansionModule,
  ],
  template: `
    <mat-sidenav-container class="app-container">
      <mat-sidenav #sidenav mode="side" opened class="app-sidenav">

        <div class="sidenav-header">
          <mat-icon class="logo-icon">security</mat-icon>
          <span class="logo-text">TrueNorth</span>
        </div>

        <nav class="side-nav">
          <mat-accordion multi displayMode="flat">
            @for (section of navSections; track section.name) {
              <mat-expansion-panel [expanded]="true" hideToggle="false">
                <mat-expansion-panel-header>
                  <mat-panel-title>{{ section.name }}</mat-panel-title>
                </mat-expansion-panel-header>
                <mat-nav-list dense>
                  @for (item of section.items; track item.route) {
                    <a mat-list-item
                       [routerLink]="item.route"
                       routerLinkActive="active-link"
                       [matTooltip]="item.label"
                       matTooltipPosition="right"
                       matTooltipShowDelay="600">
                      <mat-icon matListItemIcon>{{ item.icon }}</mat-icon>
                      <span matListItemTitle>{{ item.label }}</span>
                    </a>
                  }
                </mat-nav-list>
              </mat-expansion-panel>
            }
          </mat-accordion>
        </nav>

      </mat-sidenav>

      <mat-sidenav-content>
        <mat-toolbar class="app-toolbar">
          <button mat-icon-button (click)="sidenav.toggle()" matTooltip="Toggle nav">
            <mat-icon>menu</mat-icon>
          </button>

          <span class="spacer"></span>

          <div class="theme-picker">
            <span class="theme-label">Theme</span>
            @for (t of themes; track t.id) {
              <button
                class="theme-btn"
                [class.active]="activeTheme === t.id"
                [matTooltip]="t.label"
                (click)="setTheme(t)">
                <span class="swatch-half left" [style.background]="t.colorLeft"></span>
                <span class="swatch-half right" [style.background]="t.colorRight"></span>
              </button>
            }
          </div>

          <button mat-icon-button matTooltip="Notifications">
            <mat-icon>notifications</mat-icon>
          </button>
          <button mat-icon-button matTooltip="Account">
            <mat-icon>account_circle</mat-icon>
          </button>
        </mat-toolbar>

        <main class="app-content">
          <router-outlet />
        </main>
      </mat-sidenav-content>
    </mat-sidenav-container>
  `,
  styles: [`
    .app-container  { height: 100vh; }

    /* ── Sidenav ─────────────────────────────────────── */
    .app-sidenav {
      width: 240px;
      display: flex;
      flex-direction: column;
      background: var(--sidenav-bg) !important;
      border-right: 1px solid var(--border) !important;
    }

    .sidenav-header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 16px 14px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    .logo-icon { font-size: 28px; width: 28px; height: 28px; color: var(--accent); }
    .logo-text  { font-size: 16px; font-weight: 600; color: var(--text-primary); letter-spacing: 0.3px; }

    /* ── Nav sections ────────────────────────────────── */
    .side-nav {
      flex: 1;
      overflow-y: auto;
      padding: 6px 0 16px;
    }

    /* ── Toolbar ─────────────────────────────────────── */
    .app-toolbar {
      position: sticky;
      top: 0;
      z-index: 10;
      height: 56px !important;
      min-height: 56px !important;
      padding: 0 8px;
      background: var(--toolbar-bg) !important;
      border-bottom: 1px solid var(--border);
    }

    .app-content {
      min-height: calc(100vh - 56px);
      background: var(--bg-primary);
    }

    /* ── Active nav link ─────────────────────────────── */
    a.active-link {
      background: var(--accent-muted) !important;
    }
    a.active-link .mat-icon { color: var(--accent) !important; }
    a.active-link span       { color: var(--accent) !important; }

    .spacer { flex: 1 1 auto; }

    /* ── Theme picker ────────────────────────────────── */
    .theme-picker {
      display: flex; align-items: center; gap: 8px; margin-right: 8px;
    }
    .theme-label {
      font-size: 11px; color: var(--text-muted); text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .theme-btn {
      width: 26px; height: 26px; border-radius: 50%; border: 2px solid transparent;
      padding: 0; cursor: pointer; display: flex; overflow: hidden;
      transition: border-color 0.15s; background: none;
    }
    .theme-btn.active { border-color: var(--accent); }
    .theme-btn:hover  { border-color: var(--border-light); }
    .swatch-half { flex: 1; height: 100%; }

    /* ── Expansion panel overrides (component-scoped) ── */
    :host ::ng-deep .side-nav .mat-accordion .mat-expansion-panel {
      background: transparent !important;
      box-shadow: none !important;
      border-radius: 0 !important;
      margin: 0 !important;
    }
    :host ::ng-deep .side-nav .mat-expansion-panel-header {
      padding: 0 16px !important;
      height: 36px !important;
      min-height: 36px !important;
    }
    :host ::ng-deep .side-nav .mat-expansion-panel-body {
      padding: 0 !important;
    }
    :host ::ng-deep .side-nav .mat-expansion-panel-content {
      padding: 0 !important;
    }
    :host ::ng-deep .side-nav .mat-expansion-panel::before,
    :host ::ng-deep .side-nav .mat-expansion-panel:not(:first-child)::before {
      display: none !important;
    }
  `],
})
export class AppComponent {
  activeTheme = 'northern-ops';

  themes: ThemeOption[] = [
    { id: 'northern-ops', label: 'Northern Ops',  className: 'theme-northern-ops', colorLeft: '#0B1F3A', colorRight: '#1FB6A6' },
    { id: 'maple-steel',  label: 'Maple & Steel', className: 'theme-maple-steel',  colorLeft: '#0D1117', colorRight: '#D62828' },
    { id: 'aurora-soc',   label: 'Aurora SOC',    className: 'theme-aurora-soc',   colorLeft: '#070A12', colorRight: '#2DE2C5' },
  ];

  navSections: NavSection[] = [
    {
      name: 'Range Operations',
      items: [
        { label: 'Dashboard',    icon: 'dashboard',     route: '/dashboard' },
        { label: 'Ranges',       icon: 'dns',           route: '/ranges' },
        { label: 'Exercises',    icon: 'fitness_center',route: '/exercises' },
        { label: 'Telemetry',    icon: 'leaderboard',   route: '/telemetry' },
        { label: 'Scoring',      icon: 'assessment',    route: '/scoring' },
        { label: 'Ops Center',   icon: 'radar',         route: '/ops-center/select' },
      ],
    },
    {
      name: 'Design',
      items: [
        { label: 'Range Designer',   icon: 'architecture',  route: '/range-designer' },
        { label: 'Templates',        icon: 'description',   route: '/templates' },
        { label: 'Scenario Builder', icon: 'build',         route: '/scenario-builder' },
        { label: 'Scenarios',        icon: 'play_circle',   route: '/scenarios' },
        { label: 'Exercise Forge',   icon: 'auto_fix_high', route: '/exercise-forge' },
        { label: 'Detection Editor', icon: 'shield',        route: '/detection-editor' },
      ],
    },
    {
      name: 'Training',
      items: [
        { label: 'LMS',            icon: 'school',        route: '/training' },
        { label: 'Competency',     icon: 'psychology',    route: '/competency' },
        { label: 'Content Catalog',icon: 'library_books', route: '/content' },
      ],
    },
    {
      name: 'Platform',
      items: [
        { label: 'Infrastructure',  icon: 'storage',             route: '/infrastructure' },
        { label: 'AI Orchestrator', icon: 'memory',              route: '/ai-orchestrator' },
        { label: 'Users',           icon: 'people',              route: '/users' },
        { label: 'Integrations',    icon: 'device_hub',          route: '/integrations' },
        { label: 'Admin',           icon: 'admin_panel_settings',route: '/admin' },
      ],
    },
  ];

  constructor(private renderer: Renderer2) {
    const saved = localStorage.getItem('tn-theme') || 'northern-ops';
    this.setThemeById(saved);
  }

  setTheme(theme: ThemeOption): void {
    this.setThemeById(theme.id);
    localStorage.setItem('tn-theme', theme.id);
  }

  private setThemeById(id: string): void {
    this.activeTheme = id;
    const body = document.body;
    this.themes.forEach(t => body.classList.remove(t.className));
    const found = this.themes.find(t => t.id === id);
    if (found) body.classList.add(found.className);
  }
}