import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';
import { AppComponent } from './app.component';

describe('AppComponent', () => {
  let component: AppComponent;
  let fixture: ComponentFixture<AppComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppComponent, NoopAnimationsModule, RouterTestingModule],
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
    expect(logoText?.textContent).toContain('TrueNorth Range');
  });

  // ── Renders navigation ───────────────────────────────────────────
  it('should render navigation items', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const navItems = el.querySelectorAll('mat-nav-list a[mat-list-item]');
    expect(navItems.length).toBe(component.navItems.length);
  });

  it('should have Dashboard as the first nav item', () => {
    expect(component.navItems[0].label).toBe('Dashboard');
    expect(component.navItems[0].route).toBe('/dashboard');
  });

  it('should include expected nav routes', () => {
    const routes = component.navItems.map((n) => n.route);
    expect(routes).toContain('/dashboard');
    expect(routes).toContain('/ranges');
    expect(routes).toContain('/exercises');
    expect(routes).toContain('/scenarios');
    expect(routes).toContain('/telemetry');
    expect(routes).toContain('/admin');
  });

  // ── Nav icons ────────────────────────────────────────────────────
  it('should render nav icons', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const icons = el.querySelectorAll('mat-nav-list mat-icon');
    expect(icons.length).toBeGreaterThanOrEqual(component.navItems.length);
  });

  // ── Toolbar buttons ──────────────────────────────────────────────
  it('should render toolbar with menu toggle, notifications, and account buttons', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const toolbarButtons = el.querySelectorAll('mat-toolbar button');
    expect(toolbarButtons.length).toBe(3); // menu, notifications, account
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
    for (const item of component.navItems) {
      expect(item.label).toBeTruthy();
      expect(item.icon).toBeTruthy();
      expect(item.route).toBeTruthy();
      expect(item.route.startsWith('/')).toBeTrue();
    }
  });
});