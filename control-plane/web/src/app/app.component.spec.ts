import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';
import { AppComponent } from './app.component';
import { AuthService } from './core/services/auth.service';

describe('AppComponent', () => {
  let component: AppComponent;
  let fixture: ComponentFixture<AppComponent>;
  const canViewSchedule = signal(true);

  beforeEach(async () => {
    canViewSchedule.set(true);
    await TestBed.configureTestingModule({
      imports: [AppComponent, NoopAnimationsModule, RouterTestingModule],
      providers: [{ provide: AuthService, useValue: { canViewSchedule } }],
    }).compileComponents();

    fixture = TestBed.createComponent(AppComponent);
    component = fixture.componentInstance;
  });

  // ── Creation ─────────────────────────────────────────────────────
  it('should create the app', () => {
    expect(component).toBeTruthy();
  });

  // ── Has title 'TrueNorth Range' ──────────────────────────────────
  it("should have title 'TrueNorth Range' in the sidenav header", () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const logoText = el.querySelector('.logo-text');
    expect(logoText?.textContent).toContain('TrueNorth');
  });

  // ── Renders navigation ───────────────────────────────────────────
  it('should render navigation items', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const navItems = el.querySelectorAll('mat-nav-list a[mat-list-item]');
    const allItems = component.navSections.flatMap(s => s.items);
    expect(navItems.length).toBe(allItems.length);
  });

  it('should have Dashboard as the first nav item', () => {
    const firstItem = component.navSections[0].items[0];
    expect(firstItem.label).toBe('Dashboard');
    expect(firstItem.route).toBe('/dashboard');
  });

  it('should include expected nav routes', () => {
    const routes = component.navSections.flatMap(s => s.items.map(n => n.route));
    expect(routes).toContain('/dashboard');
    expect(routes).toContain('/exercises');
    // Scenarios live in the Authoring Studio hub (/scenarios redirects there), and
    // live telemetry sits in the Ops Center — neither has its own nav entry now.
    expect(routes).toContain('/authoring');
    expect(routes).toContain('/ops-center/select');
    expect(routes).toContain('/learning');
    expect(routes).toContain('/admin');
  });

  // ── Nav icons ────────────────────────────────────────────────────
  it('should render nav icons', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const icons = el.querySelectorAll('mat-nav-list mat-icon');
    const totalItems = component.navSections.reduce((sum, s) => sum + s.items.length, 0);
    expect(icons.length).toBeGreaterThanOrEqual(totalItems);
  });

  // ── Toolbar buttons ──────────────────────────────────────────────
  it('should give every toolbar button an accessible name', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const toolbarButtons = el.querySelectorAll('mat-toolbar button');
    expect(toolbarButtons.length).toBeGreaterThan(0);
    toolbarButtons.forEach(button => expect(button.getAttribute('aria-label')).toBeTruthy());
  });

  // ── Sidenav ──────────────────────────────────────────────────────
  it('should have an opened sidenav by default', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const sidenav = el.querySelector('mat-sidenav');
    // The sidenav should exist
    expect(sidenav).toBeTruthy();
  });

  // ── Router outlet ────────────────────────────────────────────────
  it('should contain a router-outlet', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const outlet = el.querySelector('router-outlet');
    expect(outlet).toBeTruthy();
  });

  // ── NavItem interface ────────────────────────────────────────────
  it('each navItem should have label, icon, and route', () => {
    for (const section of component.navSections) {
      for (const item of section.items) {
        expect(item.label).toBeTruthy();
        expect(item.icon).toBeTruthy();
        expect(item.route).toBeTruthy();
        expect(item.route.startsWith('/')).toBeTrue();
      }
    }
  });

  it('hides Schedule from Students (ADR 0004)', () => {
    fixture.detectChanges();
    const routes = () => Array.from(fixture.nativeElement.querySelectorAll('mat-nav-list a[mat-list-item]') as NodeListOf<HTMLAnchorElement>)
      .map(a => a.getAttribute('href'));
    expect(routes()).toContain('/schedule');
    canViewSchedule.set(false);
    fixture.detectChanges();
    expect(routes()).not.toContain('/schedule');
  });
});
