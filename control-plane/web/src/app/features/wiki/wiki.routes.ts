import { Routes } from '@angular/router';
import { kbStaffGuard } from '@shared/kb-roles';

const editors = [kbStaffGuard('/wiki')];

/** `/wiki/...` — mounted from app.routes.ts behind authGuard + onboardingGuard. */
export const WIKI_ROUTES: Routes = [
  {
    path: '',
    loadComponent: () => import('./wiki-home.component').then(m => m.WikiHomeComponent),
    title: 'Wiki - TrueNorth Range',
  },
  {
    path: ':space',
    loadComponent: () => import('./wiki-space.component').then(m => m.WikiSpaceComponent),
    title: 'Wiki - TrueNorth Range',
    children: [
      {
        path: '',
        loadComponent: () => import('./wiki-space-landing.component').then(m => m.WikiSpaceLandingComponent),
      },
      {
        path: 'new',
        canActivate: editors,
        loadComponent: () => import('./wiki-edit.component').then(m => m.WikiEditComponent),
        title: 'New page - TrueNorth Range',
      },
      {
        path: ':pageId',
        loadComponent: () => import('./wiki-page.component').then(m => m.WikiPageComponent),
      },
      {
        path: ':pageId/edit',
        canActivate: editors,
        loadComponent: () => import('./wiki-edit.component').then(m => m.WikiEditComponent),
        title: 'Edit page - TrueNorth Range',
      },
      {
        path: ':pageId/history',
        canActivate: editors,
        loadComponent: () => import('./wiki-history.component').then(m => m.WikiHistoryComponent),
        title: 'Page history - TrueNorth Range',
      },
    ],
  },
];
