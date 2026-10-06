import { Component, ElementRef, NgZone, OnDestroy, ViewChild, signal, inject } from '@angular/core';
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
import { TourOverlayComponent } from './shared/tour/tour-overlay.component';

interface NavItem {
  label: string;
  icon: string;
  route: string;
}

interface NavSection {
  name: string;
  items: NavItem[];
}

/** Full-screen pages: sign-in, and the ARC² Course Studio, which has its own chrome. */
const BARE_ROUTES = ['/login', '/arc2'];
const isBareRoute = (url: string): boolean =>
  BARE_ROUTES.some(r => url === r || url.startsWith(r + '/') || url.startsWith(r + '?'));

@Component({
  selector: 'tn-root',
  imports: [
    RouterModule,
    RouterOutlet,
    TourOverlayComponent,
    MatSidenavModule,
    MatToolbarModule,
    MatListModule,
    MatIconModule,
    MatButtonModule,
    MatTooltipModule,
    MatExpansionModule
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
        <mat-sidenav #sidenav [mode]="mobile() ? 'over' : 'side'" [opened]="!mobile()"
                     class="app-sidenav" [class.collapsed]="!mobile() && collapsed()">

          <div class="sidenav-header">
            <span class="logo-icon" aria-hidden="true">🇨🇦</span>
            <span class="logo-text">True<span class="brand-accent">North</span></span>
          </div>

          <nav class="side-nav" #sideNavEl aria-label="TrueNorth workspaces">
            <span class="active-indicator" #indicator></span>
              @for (section of navSections; track section.name) {
                <section class="nav-group" [attr.aria-label]="section.name">
                  <h2 class="nav-group-title">{{ section.name }}</h2>
                  <mat-nav-list dense>
                    @for (item of section.items; track item.route) {
                      <a mat-list-item
                         [routerLink]="item.route"
                         (click)="mobile() && sidenav.close()"
                         [attr.data-tour]="item.route"
                         [attr.aria-label]="item.label"
                         ariaCurrentWhenActive="page"
                         routerLinkActive="active-link"
                         [matTooltip]="item.label"
                         matTooltipPosition="right"
                         [matTooltipShowDelay]="collapsed() ? 100 : 600">
                        <mat-icon matListItemIcon>{{ item.icon }}</mat-icon>
                        <span matListItemTitle class="nav-label">{{ item.label }}</span>
                      </a>
                    }
                  </mat-nav-list>
                </section>
              }
          </nav>
          @if (!collapsed()) {
            <p class="workspace-note">One workspace.<br>One clear place for each task.</p>
          }

        </mat-sidenav>

        <mat-sidenav-content>
          <mat-toolbar class="app-toolbar">
            <button mat-icon-button (click)="mobile() ? sidenav.toggle() : toggleRail()" matTooltip="Toggle navigation"
                    aria-label="Toggle navigation" [attr.aria-expanded]="mobile() ? sidenav.opened : !collapsed()">
              <mat-icon>{{ collapsed() ? 'menu_open' : 'menu' }}</mat-icon>
            </button>
            <span class="toolbar-kicker">Workspace <span class="kicker-sep">/</span> <strong>{{ workspaceTitle() }}</strong></span>

            <span class="spacer"></span>

            <details class="appearance-menu">
              <summary>Appearance</summary>
              <div class="theme-picker">
              @for (t of themes; track t.id) {
                <button
                  class="theme-btn"
                  [class.active]="activeTheme() === t.id"
                  [attr.aria-label]="t.label + ' theme'"
                  [attr.aria-pressed]="activeTheme() === t.id"
                  [matTooltip]="t.label"
                  (click)="setTheme(t)">
                  <span class="swatch-half left" [style.background]="t.colorLeft"></span>
                  <span class="swatch-half right" [style.background]="t.colorRight"></span>
                </button>
              }
              </div>
            </details>

          </mat-toolbar>

          <main class="app-content" #content>
            <router-outlet />
          </main>
        </mat-sidenav-content>
      </mat-sidenav-container>

      <!-- Rendered here so a tour can highlight the nav as well as the page. -->
      <tn-tour-overlay />
    }
  `,
  styles: [`
    .app-container  { height: 100vh; background: transparent !important; }

    /* ── Sidenav rail ────────────────────────────────── */
    .app-sidenav {
      width: 210px;
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
      padding: 18px 22px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      white-space: nowrap;
    }
    .logo-icon {
      font-size: 28px; width: 28px; height: 28px; flex-shrink: 0;
      color: var(--accent);
      line-height: 28px;
    }
    .logo-text {
      font-family: var(--font-display);
      font-size: 17px; font-weight: 600; color: var(--text-primary);
      transition: opacity 0.2s ease;
    }
    .collapsed .logo-text { opacity: 0; }
    .brand-accent { color: var(--accent); }
    .nav-group { margin: 0 10px 14px; }
    .nav-group-title { margin: 14px 12px 4px; font-size: 10px; font-weight: 600; letter-spacing: .8px; text-transform: uppercase; color: var(--text-muted); }
    .collapsed .nav-group-title { visibility: hidden; height: 4px; margin: 8px 0; }
    .workspace-note { padding: 12px 22px; font-size: 12px; line-height: 1.6; color: var(--text-muted); }

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
      height: 64px !important;
      min-height: 64px !important;
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
      font-size: 13px;
      font-weight: 400;
      letter-spacing: 0;
      color: var(--text-muted);
      user-select: none;
    }
    .toolbar-kicker .kicker-sep { color: var(--border); margin: 0 12px; }
    .toolbar-kicker strong { color: var(--text-primary); font-weight: 600; }

    .app-content {
      min-height: calc(100vh - 64px);
      background: transparent;
      position: relative;
      z-index: 1;
    }
    @media (max-width: 720px) {
      .app-sidenav { width: 180px; }
      .toolbar-kicker { letter-spacing: 0; font-size: 12px; }
      .theme-label { display: none; }
      .theme-picker { gap: 4px; }
    }

    /* ── Active nav link ─────────────────────────────── */
    a.active-link {
      background: var(--accent-muted) !important;
      box-shadow: inset 3px 0 0 var(--accent);
    }
    a.active-link .mat-icon { color: var(--accent) !important; }
    a.active-link span       { color: var(--accent) !important; }

    .spacer { flex: 1 1 auto; }

    /* ── Theme picker ────────────────────────────────── */
    .theme-picker {
      display: flex; align-items: center; gap: 8px; margin-right: 8px;
    }
    .appearance-menu { position: relative; font: 12px var(--font-body); color: var(--text-muted); margin-right: 12px; }
    .appearance-menu summary { cursor: pointer; padding: 8px; border: 1px solid var(--border); border-radius: 6px; }
    .appearance-menu .theme-picker { position: absolute; right: 0; top: 40px; z-index: 20; padding: 12px; margin: 0; background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; }
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
  private theme = inject(ThemeService);
  private motion = inject(MotionService);
  private router = inject(Router);
  private zone = inject(NgZone);

  @ViewChild('content') contentEl?: ElementRef<HTMLElement>;
  @ViewChild('indicator') indicatorEl?: ElementRef<HTMLElement>;
  @ViewChild('sideNavEl') sideNavRef?: ElementRef<HTMLElement>;

  collapsed = signal(false);
  private readonly mobileQuery = window.matchMedia('(max-width: 720px)');
  readonly mobile = signal(this.mobileQuery.matches);
  private readonly onViewportChange = (event: MediaQueryListEvent) => {
    this.zone.run(() => this.mobile.set(event.matches));
  };
  navLoading = signal(false);
  isBareRoute = signal(false);
  workspaceTitle = signal('Overview');

  readonly themes: ThemeOption[];
  readonly activeTheme;

  navSections: NavSection[] = [
    {
      name: 'Overview',
      items: [
        { label: 'Dashboard',  icon: 'dashboard',      route: '/dashboard' },
      ],
    },
    {
      name: 'Learn',
      items: [
        { label: 'Learning',         icon: 'school',        route: '/learning' },
      ],
    },
    {
      name: 'Create',
      items: [
        { label: 'Authoring Studio', icon: 'auto_fix_high', route: '/authoring' },
      ],
    },
    {
      name: 'Prepare & Operate',
      items: [
        { label: 'Exercises', icon: 'fitness_center', route: '/exercises' },
        { label: 'Ops Center', icon: 'radar', route: '/ops-center/select' },
      ],
    },
    {
      name: 'Review',
      items: [{ label: 'Scoring & AAR', icon: 'assessment', route: '/scoring' }],
    },
    {
      name: 'Help',
      items: [
        { label: 'Wiki',    icon: 'menu_book',      route: '/wiki' },
        { label: 'Support', icon: 'support_agent',  route: '/support' },
      ],
    },
    {
      name: 'Admin',
      items: [
        { label: 'Infrastructure',  icon: 'storage',              route: '/infrastructure' },
        { label: 'AI Orchestrator', icon: 'memory',               route: '/ai-orchestrator' },
        { label: 'Integrations',    icon: 'device_hub',           route: '/integrations' },
        { label: 'Users',           icon: 'people',               route: '/users' },
        { label: 'Admin',           icon: 'admin_panel_settings', route: '/admin' },
      ],
    },
  ];

  private routerSub: Subscription;

  constructor() {
    this.themes = this.theme.themes;
    this.mobileQuery.addEventListener('change', this.onViewportChange);
    this.activeTheme = this.theme.activeTheme;
    this.isBareRoute.set(isBareRoute(this.router.url));

    this.routerSub = this.router.events.subscribe((event) => {
      if (event instanceof NavigationStart) {
        this.navLoading.set(true);
      } else if (event instanceof NavigationEnd) {
        this.navLoading.set(false);
        const path = event.urlAfterRedirects.split('/')[1]?.split('?')[0];
        this.workspaceTitle.set(({
          learning: 'Learning', authoring: 'Authoring', exercises: 'Exercises',
          'ops-center': 'Ops Center', scoring: 'Review', infrastructure: 'Infrastructure',
          'ai-orchestrator': 'AI Orchestrator', integrations: 'Integrations', users: 'People',
          admin: 'Administration',
        } as Record<string, string>)[path] ?? 'Overview');
        this.isBareRoute.set(isBareRoute(event.urlAfterRedirects));
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
    this.mobileQuery.removeEventListener('change', this.onViewportChange);
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
