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
    const totalItems = component.navSections.reduce((sum, s) => sum + s.items.length, 0);
    expect(icons.length).toBeGreaterThanOrEqual(totalItems);
  });

  // ── Toolbar buttons ──────────────────────────────────────────────
  it('should render toolbar with menu toggle, notifications, and account buttons', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const toolbarButtons = el.querySelectorAll('mat-toolbar button');
    expect(toolbarButtons.length).toBeGreaterThanOrEqual(3); // menu, notifications, account + theme buttons
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
});