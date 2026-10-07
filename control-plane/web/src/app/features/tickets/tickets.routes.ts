import { Routes } from '@angular/router';
import { adminGuard } from '@core/guards/auth.guard';
import { kbStaffGuard } from '@shared/kb-roles';

/** `/support/...` (API: /tickets) — mounted from app.routes.ts behind authGuard + onboardingGuard. */
export const SUPPORT_ROUTES: Routes = [
  {
    path: '',
    loadComponent: () => import('./ticket-list.component').then(m => m.TicketListComponent),
    title: 'Support - TrueNorth Range',
  },
  {
    path: 'new',
    loadComponent: () => import('./ticket-new.component').then(m => m.TicketNewComponent),
    title: 'Report a problem - TrueNorth Range',
  },
  {
    path: 'board',
    canActivate: [kbStaffGuard('/support')],
    loadComponent: () => import('./ticket-board.component').then(m => m.TicketBoardComponent),
    title: 'Support board - TrueNorth Range',
  },
  {
    path: 'queues',
    canActivate: [adminGuard],
    loadComponent: () => import('./ticket-queues.component').then(m => m.TicketQueuesComponent),
    title: 'Support queues - TrueNorth Range',
  },
  {
    path: ':id',
    loadComponent: () => import('./ticket-detail.component').then(m => m.TicketDetailComponent),
    title: 'Ticket - TrueNorth Range',
  },
];
