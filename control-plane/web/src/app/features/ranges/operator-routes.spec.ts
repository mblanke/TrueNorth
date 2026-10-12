import { TestBed } from '@angular/core/testing';
import { Router, RoutesRecognized, provideRouter } from '@angular/router';
import { routes } from '../../app.routes';

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
});
