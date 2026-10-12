import { TestBed } from '@angular/core/testing';
import { Router, UrlTree, provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { firstValueFrom, Observable } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { rangeStaffGuard } from './range-staff.guard';

describe('rangeStaffGuard', () => {
  async function run(role: UserRole | null): Promise<boolean | UrlTree> {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AuthService, useValue: { bootstrap: () => Promise.resolve(null),
          user: signal(role ? { id: 'u', role } : null) } },
      ],
    });
    const out = TestBed.runInInjectionContext(() => rangeStaffGuard({} as any, {} as any)) as Observable<boolean | UrlTree>;
    return firstValueFrom(out);
  }

  it('lets staff and observers in', async () => {
    for (const role of ['admin', 'instructor', 'range_ops', 'observer'] as UserRole[]) {
      expect(await run(role)).toBe(true);
    }
  });

  it('sends a Student, or nobody, to the dashboard', async () => {
    for (const role of ['student', null] as (UserRole | null)[]) {
      const out = await run(role);
      expect(out instanceof UrlTree).toBeTrue();
      expect(TestBed.inject(Router).serializeUrl(out as UrlTree)).toBe('/dashboard');
    }
  });
});
