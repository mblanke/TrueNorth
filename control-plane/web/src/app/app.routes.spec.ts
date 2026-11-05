import { Route } from '@angular/router';
import { routes } from './app.routes';
import { authGuard, onboardingGuard } from './core/guards/auth.guard';

/** Every statically declared route with its full path; a parameter name is normalized to
 *  `:param`, so `x/:id` and `x/:slug` count as one path. Lazily loaded child route files
 *  (`loadChildren`) are not walked here. */
function fullPaths(list: Route[], parent = ''): { path: string; route: Route }[] {
  return list.flatMap(route => {
    const own = (route.path ?? '').split('/').filter(Boolean)
      .map(seg => (seg.startsWith(':') ? ':param' : seg)).join('/');
    const path = [parent, own].filter(Boolean).join('/');
    // A child at '' is the parent's own default (`authoring` -> `authoring/ranges`), not a
    // second declaration of the parent's path.
    const here = route.path === undefined || (parent && !own) ? [] : [{ path, route }];
    return [...here, ...fullPaths(route.children ?? [], path)];
  });
}

describe('app routes (codereview1 R3-07)', () => {
  it('declares every full path once', () => {
    const seen = new Map<string, number>();
    for (const { path } of fullPaths(routes)) seen.set(path, (seen.get(path) ?? 0) + 1);
    const dupes = [...seen].filter(([, n]) => n > 1).map(([p, n]) => `${p || '(root)'} x${n}`);
    expect(dupes).withContext('the router only ever uses the first; the rest is drift').toEqual([]);
  });

  it('keeps the exercise template pages, with their guards, ahead of exercises/:id', () => {
    const top = routes.map(r => r.path);
    for (const path of ['exercises/templates', 'exercises/templates/:id']) {
      const route = routes.find(r => r.path === path);
      expect(route).withContext(path).toBeDefined();
      expect(route!.canActivate?.length).withContext(`${path} guards`).toBe(3);
      expect(route!.canActivate).toContain(authGuard);
      expect(route!.canActivate).toContain(onboardingGuard);
      expect(route!.loadComponent).withContext(`${path} component`).toBeDefined();
      expect(top.indexOf(path)).toBeLessThan(top.indexOf('exercises/:id'));
    }
  });
});
