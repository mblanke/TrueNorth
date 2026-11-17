import { Routes } from '@angular/router';

import { routes } from './app.routes';

/** Full paths of every route in the table, including nested children. Index routes ('') are skipped. */
function fullPaths(table: Routes, prefix = ''): string[] {
  return table.flatMap(r => {
    const path = [prefix, r.path ?? ''].filter(Boolean).join('/');
    return [...(r.path ? [path] : []), ...(r.children ? fullPaths(r.children, path) : [])];
  });
}

describe('app routes', () => {
  it('registers no path twice (the router only ever matches the first)', () => {
    const seen = new Set<string>();
    const duplicates = fullPaths(routes).filter(p => (seen.has(p) ? true : (seen.add(p), false)));
    expect(duplicates).toEqual([]);
  });

  it('registers each exercise-templates route once, ahead of exercises/:id', () => {
    const paths = fullPaths(routes);
    expect(paths.filter(p => p === 'exercises/templates').length).toBe(1);
    expect(paths.filter(p => p === 'exercises/templates/:id').length).toBe(1);
  });
});
