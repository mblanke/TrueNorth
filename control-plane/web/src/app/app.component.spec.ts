import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';
import { of } from 'rxjs';
import { AppComponent } from './app.component';
import { AuthService, UserRole } from './core/services/auth.service';
import { fakeAuth } from './core/auth/fake-auth';
import { PAGE_ACCESS, roleHasAccess } from './core/auth/permissions';
import { NotificationsApiService } from '@core/services/notifications-api.service';
import { EmailStatusService } from '@core/services/email-status.service';
import { signal } from '@angular/core';

describe('AppComponent', () => {
  let component: AppComponent;
  let fixture: ComponentFixture<AppComponent>;
  let auth: ReturnType<typeof fakeAuth>;
  const emailOff = signal(false);

  beforeEach(async () => {
    auth = fakeAuth('admin');
    await TestBed.configureTestingModule({
      imports: [AppComponent, NoopAnimationsModule, RouterTestingModule],
      providers: [
        { provide: AuthService, useValue: auth },
        { provide: NotificationsApiService, useValue: { unreadCount: () => of({ unread: 0 }), list: () => of([]) } },
        // The banner's own spec covers it (shared/email-banner.component.spec.ts).
        { provide: EmailStatusService, useValue: { load: () => undefined, notConfigured: emailOff } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(AppComponent);
    component = fixture.componentInstance;
  });

  // ── Email not configured (#306) ──────────────────────────────────
  it('shows admins the email banner above every page while email is off', () => {
    emailOff.set(true);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('main .email-banner')?.textContent).toContain('Email is not configured');
    emailOff.set(false);
    fixture.detectChanges();
    expect(el.querySelector('.email-banner')).toBeNull();
  });

  // ── A seat lost on a class (ADR 0014 §4) ────────────────────────
  it('tells a signed-in Student which class seat they lost, until dismissed', () => {
    auth.role.set('student');
    auth.me.set({ roll_call_forfeited: ['C201-26-03'] });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.seat-notice')?.textContent).toContain('You lost your seat on C201-26-03');
    expect(el.querySelector('.seat-notice')?.textContent).toContain('Ask your instructor');
    (el.querySelector('.seat-notice button') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(el.querySelector('.seat-notice')).toBeNull();
  });

  it('shows no seat notice when nothing was lost', () => {
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.seat-notice')).toBeNull();
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
    // An admin sees every entry except My classes, whose staff counterpart is Classes (notWith).
    const allItems = component.navSections.flatMap(s => s.items).filter(n => !n.notWith);
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
    const totalItems = component.navSections.reduce((sum, s) => sum + s.items.filter(n => !n.notWith).length, 0);
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

  // ── Sign out (#318) ─────────────────────────────────────────────
  it('offers Sign out in the toolbar, which ends the session', () => {
    const logout = spyOn(auth, 'logout');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const button = el.querySelector('mat-toolbar button[aria-label="Sign out"]') as HTMLButtonElement;
    expect(button).not.toBeNull();
    expect(button.textContent).toContain('Sign out');
    button.click();
    expect(logout).toHaveBeenCalled();
  });

  it('shows no Sign out to someone not signed in', () => {
    auth.role.set(null);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('button[aria-label="Sign out"]')).toBeNull();
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
    // My classes (class:attend, ADR 0014 §20): Students. Not /classes: that page is staff only;
    // Students reach their range through My classes' Join.
    student: ['/dashboard', '/learning', '/my-classes', '/exercises', '/scoring', '/wiki', '/support'],
    // The calendar's Events view (schedule:read, #285): observers too, read-only.
    // Unit readiness (readiness:read, #289): commander = observer + this page (ADR 0014 decision 7).
    observer: ['/dashboard', '/learning', '/exercises', '/ops-center/select', '/telemetry', '/schedule',
      '/calendar', '/classes', '/scoring', '/readiness', '/wiki'],
    range_ops: ['/dashboard', '/learning', '/authoring', '/exercises', '/ops-center/select', '/telemetry',
      '/schedule', '/calendar', '/wiki', '/support', '/infrastructure', '/ai-orchestrator'],
    // Student progress (learning_record:read) and Teams (user:read): instructors and admins.
    instructor: ['/dashboard', '/learning', '/learning/admin/progress', '/authoring', '/exercises',
      '/ops-center/select', '/telemetry', '/schedule', '/calendar', '/classes', '/scoring', '/readiness', '/wiki',
      '/support',
      '/approvals', '/teams',
      '/infrastructure', '/ai-orchestrator'],
    admin: ['/dashboard', '/learning', '/learning/admin/progress', '/authoring', '/exercises',
      '/ops-center/select', '/telemetry', '/schedule', '/calendar', '/classes', '/scoring', '/readiness', '/wiki',
      '/support',
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

  /**
   * #283 (attendance) and #284 (My classes) together: Classes is staff only again
   * (class:read), My classes is class:attend, and Students reach their range through My
   * classes' Join. With #283's old `classes: { any: [..., 'detection:submit'] }` stand-in,
   * `notWith: 'classes'` hid My classes from every Student.
   */
  it('gives a Student My classes and not Classes, with both PRs\' permissions applied', () => {
    expect(PAGE_ACCESS.classes).toEqual({ all: ['class:read'] });
    expect(PAGE_ACCESS.myClasses).toEqual({ all: ['class:attend'] });
    expect(roleHasAccess('student', PAGE_ACCESS.classes)).toBeFalse();
    expect(roleHasAccess('student', PAGE_ACCESS.myClasses)).toBeTrue();
    auth.role.set('student');
    fixture.detectChanges();
    expect(menu()).toContain('/my-classes');
    expect(menu()).not.toContain('/classes');
    for (const staff of ['admin', 'instructor', 'observer'] as const) {
      auth.role.set(staff);
      fixture.detectChanges();
      expect(menu()).withContext(staff).toContain('/classes');
      expect(menu()).withContext(staff).not.toContain('/my-classes');
    }
  });

  it('drops a section whose entries are all hidden', () => {
    auth.role.set('student');
    fixture.detectChanges();
    const headings = Array.from(fixture.nativeElement.querySelectorAll('.nav-group-title') as NodeListOf<HTMLElement>)
      .map(h => h.textContent!.trim());
    expect(headings).not.toContain('Create');
    expect(headings).not.toContain('Admin');
  });

  it('gives observers the unit readiness page and never Students or range ops (#289)', () => {
    for (const role of ['observer', 'instructor', 'admin'] as UserRole[]) {
      auth.role.set(role);
      fixture.detectChanges();
      expect(menu()).withContext(role).toContain('/readiness');
    }
    for (const role of ['student', 'range_ops'] as UserRole[]) {
      auth.role.set(role);
      fixture.detectChanges();
      expect(menu()).withContext(role).not.toContain('/readiness');
    }
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
