import { Component, ElementRef, NgZone, OnDestroy, ViewChild, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  NavigationCancel,
  NavigationEnd,
  NavigationError,
  NavigationStart,
  Router,
  RouterModule,
  RouterOutlet,
} from '@angular/router';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { Subscription } from 'rxjs';
import gsap from 'gsap';

import { ThemeService, ThemeOption } from './core/services/theme.service';
import { MotionService } from './shared/motion';

interface NavItem {
  label: string;
  icon: string;
  route: string;
}

interface NavSection {
  name: string;
  items: NavItem[];
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
    @if (isBareRoute()) {
      <router-outlet />
    } @else {
      <div class="tn-ambient"></div>
      @if (navLoading()) {
        <div class="tn-route-progress"></div>
      }
      <mat-sidenav-container class="app-container">
        <mat-sidenav #sidenav mode="side" opened class="app-sidenav" [class.collapsed]="collapsed()">

          <div class="sidenav-header">
            <mat-icon class="logo-icon">explore</mat-icon>
            <span class="logo-text">TrueNorth</span>
          </div>

          <nav class="side-nav" #sideNavEl>
            <span class="active-indicator" #indicator></span>
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
                         [matTooltipShowDelay]="collapsed() ? 100 : 600">
                        <mat-icon matListItemIcon>{{ item.icon }}</mat-icon>
                        <span matListItemTitle class="nav-label">{{ item.label }}</span>
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
            <button mat-icon-button (click)="toggleRail()" matTooltip="Toggle nav rail">
              <mat-icon>{{ collapsed() ? 'menu_open' : 'menu' }}</mat-icon>
            </button>
            <span class="toolbar-kicker">TRUENORTH <span class="kicker-sep">//</span> CYBER RANGE</span>

            <span class="spacer"></span>

            <div class="theme-picker">
              <span class="theme-label">Theme</span>
              @for (t of themes; track t.id) {
                <button
                  class="theme-btn"
                  [class.active]="activeTheme() === t.id"
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

          <main class="app-content" #content>
            <router-outlet />
          </main>
        </mat-sidenav-content>
      </mat-sidenav-container>
    }
  `,
  styles: [`
    .app-container  { height: 100vh; background: transparent !important; }

    /* ── Sidenav rail ────────────────────────────────── */
    .app-sidenav {
      width: 240px;
      display: flex;
      flex-direction: column;
      background: var(--sidenav-bg) !important;
      border-right: 1px solid var(--border) !important;
      transition: width 0.28s cubic-bezier(0.4, 0, 0.2, 1);
      overflow-x: hidden !important;
    }
    .app-sidenav.collapsed { width: 72px; }

    .sidenav-header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 16px 14px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      white-space: nowrap;
    }
    .logo-icon {
      font-size: 28px; width: 28px; height: 28px; flex-shrink: 0;
      color: var(--accent);
      filter: drop-shadow(0 0 8px var(--accent-muted));
    }
    .logo-text {
      font-family: var(--font-display);
      font-size: 16px; font-weight: 700; color: var(--text-primary); letter-spacing: 0.3px;
      transition: opacity 0.2s ease;
    }
    .collapsed .logo-text { opacity: 0; }

    /* ── Nav sections ────────────────────────────────── */
    .side-nav {
      flex: 1;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 6px 0 16px;
      position: relative;
    }
    .nav-label { transition: opacity 0.18s ease; }
    .collapsed .nav-label { opacity: 0; }
    .collapsed .mat-expansion-panel-header { opacity: 0; pointer-events: none; height: 12px !important; min-height: 12px !important; }

    /* GSAP-driven active route indicator */
    .active-indicator {
      position: absolute;
      left: 0;
      top: 0;
      width: 3px;
      height: 28px;
      border-radius: 0 3px 3px 0;
      background: var(--gradient-accent);
      box-shadow: var(--glow-accent);
      opacity: 0;
      pointer-events: none;
      z-index: 2;
    }

    /* ── Glass toolbar ───────────────────────────────── */
    .app-toolbar {
      position: sticky;
      top: 0;
      z-index: 10;
      height: 56px !important;
      min-height: 56px !important;
      padding: 0 8px;
      background: var(--toolbar-bg) !important;
      border-bottom: 1px solid var(--glass-border);
    }
    @supports (backdrop-filter: blur(1px)) {
      .app-toolbar {
        background: var(--glass-toolbar) !important;
        backdrop-filter: blur(14px) saturate(150%);
      }
    }

    .toolbar-kicker {
      margin-left: 6px;
      font-family: var(--font-display);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 3px;
      color: var(--text-muted);
      user-select: none;
    }
    .toolbar-kicker .kicker-sep { color: var(--accent); }

    .app-content {
      min-height: calc(100vh - 56px);
      background: transparent;
      position: relative;
      z-index: 1;
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
      transition: border-color 0.15s, box-shadow 0.2s; background: none;
    }
    .theme-btn.active { border-color: var(--accent); box-shadow: var(--glow-accent); }
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
      transition: opacity 0.18s ease;
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
export class AppComponent implements OnDestroy {
  @ViewChild('content') contentEl?: ElementRef<HTMLElement>;
  @ViewChild('indicator') indicatorEl?: ElementRef<HTMLElement>;
  @ViewChild('sideNavEl') sideNavRef?: ElementRef<HTMLElement>;

  collapsed = signal(false);
  navLoading = signal(false);
  isBareRoute = signal(false);

  readonly themes: ThemeOption[];
  readonly activeTheme;

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
        { label: '3D Topology',  icon: '3d_rotation',   route: '/topology-3d' },
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
        { label: 'Curriculum Forge', icon: 'auto_stories', route: '/curriculum-forge' },
        { label: 'LMS',            icon: 'school',        route: '/training' },
        { label: 'My Progress',    icon: 'trending_up',   route: '/my-progress' },
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

  private routerSub: Subscription;

  constructor(
    private theme: ThemeService,
    private motion: MotionService,
    private router: Router,
    private zone: NgZone,
  ) {
    this.themes = this.theme.themes;
    this.activeTheme = this.theme.activeTheme;
    this.isBareRoute.set(this.router.url.startsWith('/login'));

    this.routerSub = this.router.events.subscribe((event) => {
      if (event instanceof NavigationStart) {
        this.navLoading.set(true);
      } else if (event instanceof NavigationEnd) {
        this.navLoading.set(false);
        this.isBareRoute.set(event.urlAfterRedirects.startsWith('/login'));
        if (!this.isBareRoute()) {
          // Wait a frame so the routed component and routerLinkActive exist.
          requestAnimationFrame(() => {
            if (this.contentEl) {
              this.motion.pageEnter(this.contentEl.nativeElement);
            }
            this.moveActiveIndicator();
          });
        }
      } else if (event instanceof NavigationCancel || event instanceof NavigationError) {
        this.navLoading.set(false);
      }
    });
  }

  ngOnDestroy(): void {
    this.routerSub.unsubscribe();
  }

  toggleRail(): void {
    this.collapsed.update((c) => !c);
    // Re-seat the indicator once the rail width transition settles.
    setTimeout(() => this.moveActiveIndicator(), 300);
  }

  setTheme(t: ThemeOption): void {
    this.theme.setTheme(t.id);
  }

  private moveActiveIndicator(): void {
    const indicator = this.indicatorEl?.nativeElement;
    const nav = this.sideNavRef?.nativeElement;
    if (!indicator || !nav) return;

    const active = nav.querySelector<HTMLElement>('a.active-link');
    this.zone.runOutsideAngular(() => {
      if (!active) {
        gsap.to(indicator, { autoAlpha: 0, duration: 0.2 });
        return;
      }
      const navRect = nav.getBoundingClientRect();
      const linkRect = active.getBoundingClientRect();
      const top = linkRect.top - navRect.top + nav.scrollTop + (linkRect.height - 28) / 2;
      gsap.to(indicator, {
        autoAlpha: 1,
        y: top,
        duration: this.motion.reducedMotion() ? 0 : 0.35,
        ease: 'power3.out',
      });
    });
  }
}
