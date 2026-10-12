import { TestBed } from '@angular/core/testing';
import { Router, RoutesRecognized, provideRouter } from '@angular/router';
import { routes } from '../../app.routes';
import { PAGE_ACCESS, roleHasAccess } from '@core/auth/permissions';
import { instructorGuard } from '@core/guards/auth.guard';
import type { UserRole } from '@core/services/auth.service';

/**
 * The R0-B routes resolve to their own screens, including `learning/admin/progress`,
 * which sits under the learning hub's path but is a top-level route (the hub has no such
 * child, so the router falls through to it). Guards are stubbed out: only matching is
 * under test here; each guard has its own spec.
 */
describe('R0-B operator routes', () => {
  const strip = (rs: typeof routes): typeof routes =>
    rs.map(({ canActivate: _guards, ...r }) => ({ ...r, children: r.children ? strip(r.children) : undefined }));

  async function resolve(url: string): Promise<string | undefined> {
    TestBed.configureTestingModule({ providers: [provideRouter(strip(routes))] });
    const router = TestBed.inject(Router);
    const tree = router.parseUrl(url);
    let title: string | undefined;
    const sub = router.events.subscribe(e => {
      if (e instanceof RoutesRecognized) {
        let snap = e.state.root;
        while (snap.firstChild) snap = snap.firstChild;
        title = snap.routeConfig?.title as string | undefined;
      }
    });
    await router.navigateByUrl(tree).catch(() => undefined);
    sub.unsubscribe();
    return title;
  }

  it('ranges/:id is the range recovery screen', async () => {
    expect(await resolve('/ranges/abc')).toBe('Range recovery - TrueNorth Range');
  });

  it('teams is team membership', async () => {
    expect(await resolve('/teams')).toBe('Team membership - TrueNorth Range');
  });

  it('learning/admin/progress is Student progress, not the course admin screen', async () => {
    expect(await resolve('/learning/admin/progress')).toBe('Student progress - TrueNorth Range');
  });

  /** The keys these routes' pageGuards check, and the menu reads: who gets in. */
  it('admits each role per the permission table', () => {
    const who = (key: 'rangeRecovery' | 'teams' | 'learnerProgress') =>
      (['admin', 'instructor', 'range_ops', 'observer', 'student'] as UserRole[])
        .filter(r => roleHasAccess(r, PAGE_ACCESS[key]));
    expect(who('rangeRecovery')).toEqual(['admin', 'instructor', 'range_ops']);
    expect(who('teams')).toEqual(['admin', 'instructor']);
    expect(who('learnerProgress')).toEqual(['admin', 'instructor']);
  });

  it('guards the three routes with the permission table, not a role list', () => {
    for (const path of ['ranges/:id', 'teams', 'learning/admin/progress']) {
      const route = routes.find(r => r.path === path)!;
      expect(route.canActivate?.length).withContext(path).toBe(3);
      expect(route.canActivate).not.toContain(instructorGuard);
    }
  });
});
