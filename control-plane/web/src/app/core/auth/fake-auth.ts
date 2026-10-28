import { computed, signal } from '@angular/core';
import type { UserRole } from '../services/auth.service';
import { PAGE_ACCESS, PageKey, Permission, roleCan, roleHasAccess } from './permissions';

/**
 * A stand-in for AuthService in component specs, answering from the real permission
 * table (permissions.ts), so a spec that says "as a Student" sees exactly what a
 * Student would. `role.set(...)` switches identity mid-test. Test-only.
 */
export function fakeAuth(initial: UserRole | null) {
  const role = signal<UserRole | null>(initial);
  // GET /auth/me as the shell reads it; a spec sets what it needs (e.g. roll_call_forfeited).
  const me = signal<{ roll_call_forfeited?: string[] } | null>(null);
  // /auth/me's platform_admin: an admin of the operator's tenant. A spec sets it when it needs it.
  const platformAdmin = signal(false);
  return {
    role,
    me,
    platformAdmin,
    isPlatformAdmin: computed(() => role() === 'admin' && platformAdmin()),
    user: computed(() => (role() ? { id: 'u-self', role: role()!, sub: '', email: '', display_name: 'Me' } : null)),
    userId: computed(() => (role() ? 'u-self' : null)),
    isAuthenticated: computed(() => role() !== null),
    isAdmin: computed(() => role() === 'admin'),
    isInstructor: computed(() => role() === 'admin' || role() === 'instructor'),
    canAcknowledgeObjectives: computed(() => roleCan(role(), 'objective:ack')),
    canSubmitDetections: computed(() => roleCan(role(), 'detection:submit')),
    canAuthorCourses: computed(() => roleCan(role(), 'course:author')),
    canViewSchedule: computed(() => roleCan(role(), 'schedule:read')),
    can: (p: Permission) => roleCan(role(), p),
    canOpen: (page: PageKey) => roleHasAccess(role(), PAGE_ACCESS[page]),
    /** Spy on it (`spyOn(auth, 'logout')`); it does nothing here. */
    logout: (): void => undefined,
  };
}
