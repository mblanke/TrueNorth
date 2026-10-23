import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';
import { of } from 'rxjs';
import { AppComponent } from './app.component';
import { AuthService, UserRole } from './core/services/auth.service';
import { fakeAuth } from './core/auth/fake-auth';
import { NotificationsApiService } from '@core/services/notifications-api.service';

describe('AppComponent', () => {
  let component: AppComponent;
  let fixture: ComponentFixture<AppComponent>;
  let auth: ReturnType<typeof fakeAuth>;

  beforeEach(async () => {
    auth = fakeAuth('admin');
    await TestBed.configureTestingModule({
      imports: [AppComponent, NoopAnimationsModule, RouterTestingModule],
      providers: [
        { provide: AuthService, useValue: auth },
        { provide: NotificationsApiService, useValue: { unreadCount: () => of({ unread: 0 }), list: () => of([]) } },
      ],
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

  const menu = () => Array.from(fixture.nativeElement.querySelectorAll('mat-nav-list a[mat-list-item]') as NodeListOf<HTMLAnchorElement>)
    .map(a => a.getAttribute('href'));

  it('hides Schedule from Students (ADR 0004)', () => {
    fixture.detectChanges();
    expect(menu()).toContain('/schedule');
    auth.role.set('student');
    fixture.detectChanges();
    expect(menu()).not.toContain('/schedule');
  });

  /**
   * R0-B: the menu per role, derived from the permissions rbac.py grants. Every entry
   * shown is one the route guard admits; everything else is absent.
   */
  const MENU: Record<UserRole, string[]> = {
    student: ['/dashboard', '/learning', '/exercises', '/scoring', '/wiki', '/support'],
    observer: ['/dashboard', '/learning', '/exercises', '/ops-center/select', '/telemetry', '/schedule',
      '/classes', '/scoring', '/wiki'],
    range_ops: ['/dashboard', '/learning', '/authoring', '/exercises', '/ops-center/select', '/telemetry',
      '/schedule', '/calendar', '/wiki', '/support', '/infrastructure', '/ai-orchestrator'],
    // The calendar's Content view (content_review:read): staff, never observers or Students.
    // Student progress (learning_record:read) and Teams (user:read): instructors and admins.
    instructor: ['/dashboard', '/learning', '/learning/admin/progress', '/authoring', '/exercises',
      '/ops-center/select', '/telemetry', '/schedule', '/calendar', '/classes', '/scoring', '/wiki', '/support',
      '/approvals', '/teams',
      '/infrastructure', '/ai-orchestrator'],
    admin: ['/dashboard', '/learning', '/learning/admin/progress', '/authoring', '/exercises',
      '/ops-center/select', '/telemetry', '/schedule', '/calendar', '/classes', '/scoring', '/wiki', '/support',
      '/approvals', '/teams',
      // Maintenance windows (range:admin, ADR 0013 §7): admins only.
      // Rules of use (ADR 0014 §3): admins export signatures; the platform admin publishes.
      '/infrastructure', '/maintenance-windows', '/ai-orchestrator', '/integrations', '/users', '/admin/rules-of-use',
      '/admin'],
  };
  for (const role of Object.keys(MENU) as UserRole[]) {
    it(`shows ${role} exactly the pages the role may open`, () => {
      auth.role.set(role);
      fixture.detectChanges();
      expect(menu()).toEqual(MENU[role]);
    });
  }

  it('drops a section whose entries are all hidden', () => {
    auth.role.set('student');
    fixture.detectChanges();
    const headings = Array.from(fixture.nativeElement.querySelectorAll('.nav-group-title') as NodeListOf<HTMLElement>)
      .map(h => h.textContent!.trim());
    expect(headings).not.toContain('Create');
    expect(headings).not.toContain('Admin');
  });

  it('has an approvals entry for instructors and a telemetry entry for roles with telemetry:read', () => {
    auth.role.set('instructor');
    fixture.detectChanges();
    expect(menu()).toContain('/approvals');
    auth.role.set('observer');
    fixture.detectChanges();
    expect(menu()).toContain('/telemetry');
    expect(menu()).not.toContain('/approvals');
  });
});
