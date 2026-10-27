import { Routes } from '@angular/router';
import {
  adminGuard,
  authGuard,
  instructorGuard,
  demoGuard,
  onboardingGuard,
  pageGuard,
  pendingGuard,
  scheduleGuard,
  registrationGuard,
  rulesOfUseGuard,
} from './core/guards/auth.guard';

export const routes: Routes = [
  {
    path: '',
    redirectTo: 'dashboard',
    pathMatch: 'full',
  },
  // -- Identity intake. Reachable while authenticated but account-less, so
  //    these carry their own guards rather than authGuard. --
  {
    path: 'register',
    canActivate: [registrationGuard],
    loadComponent: () =>
      import('./features/register/register.component').then(m => m.RegisterComponent),
    title: 'Request access - TrueNorth Range',
  },
  {
    // No guard: shown when the API still wants a second factor after a step-up.
    path: 'mfa-required',
    loadComponent: () =>
      import('./features/register/mfa-required.component').then(m => m.MfaRequiredComponent),
    title: 'Multi-factor sign-in required - TrueNorth Range',
  },
  {
    // ADR 0014 §3: signed in, before anything else. Its own guard, not authGuard, which
    // sends here. Full page (app.component isBare).
    path: 'rules-of-use',
    canActivate: [rulesOfUseGuard],
    loadComponent: () =>
      import('./features/acknowledgement/rules-of-use.component').then(m => m.RulesOfUseComponent),
    title: 'Rules of use - TrueNorth Range',
  },
  {
    path: 'registration-pending',
    canActivate: [pendingGuard],
    loadComponent: () =>
      import('./features/register/registration-pending.component').then(
        m => m.RegistrationPendingComponent,
      ),
    title: 'Awaiting approval - TrueNorth Range',
  },
  {
    // authGuard only: the whole point is that onboarding is not yet complete,
    // so onboardingGuard must not apply here or it would redirect to itself.
    path: 'onboarding',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./features/onboarding/onboarding.component').then(m => m.OnboardingComponent),
    title: 'Getting started - TrueNorth Range',
  },
  {
    path: 'dashboard',
    canActivate: [authGuard, onboardingGuard],
    loadComponent: () =>
      import('./features/dashboard/dashboard.component').then(m => m.DashboardComponent),
    title: 'Dashboard - TrueNorth Range',
  },
  // Stray top-level list pages folded into the Authoring Studio hub.
  { path: 'ranges', redirectTo: 'authoring/ranges', pathMatch: 'full' },
  { path: 'templates', redirectTo: 'authoring/content', pathMatch: 'full' },
  { path: 'scenarios', redirectTo: 'authoring/scenarios', pathMatch: 'full' },
  {
    // ARC² Course Studio (course generation): full screen (isBare in app.component.ts).
    // Releases and publication stay on authoring/courses.
    path: 'arc2',
    canActivate: [authGuard, instructorGuard],
    loadComponent: () => import('./features/arc2-studio/arc2-studio.component').then(m => m.Arc2StudioComponent),
    title: 'ARC² Course Studio - TrueNorth',
  },
  // -- Authoring Studio hub (consolidates the design/authoring screens) --
  // Each tab and child route needs what its API calls need (PAGE_ACCESS in
  // core/auth/permissions.ts, mirrored from rbac.py): range_ops get Ranges and Content
  // (templates), instructors everything, Students and observers nothing.
  {
    path: 'authoring',
    canActivate: [authGuard, pageGuard('authoring')],
    loadComponent: () => import('./shared/hub-shell.component').then(m => m.HubShellComponent),
    data: {
      title: 'Authoring Studio',
      tabs: [
        // Full screen, so the tab hands over to /arc2 (see the redirect below). Hidden
        // unless the server has ARC² enabled (GET /arc2/status).
        { label: 'Course Studio', path: 'studio', requires: 'arc2', access: 'authoringCourses' },
        { label: 'Ranges', path: 'ranges', access: 'authoringRanges' },
        { label: 'Scenarios', path: 'scenarios', access: 'authoringScenarios' },
        { label: 'Detections', path: 'detections', access: 'authoringDetections' },
        { label: 'Threat intel', path: 'threat-intel', access: 'authoringThreatIntel' },
        { label: 'MESL', path: 'mesl', access: 'authoringExercises' },
        { label: 'Forge', path: 'forge', access: 'authoringExercises' },
        { label: 'Content', path: 'content', access: 'authoringContent' },
        { label: 'Courses', path: 'courses', access: 'authoringCourses' },
      ],
    },
    children: [
      { path: '', redirectTo: 'ranges', pathMatch: 'full' },
      { path: 'studio', redirectTo: '/arc2', pathMatch: 'full' },
      {
        path: 'courses',
        canActivate: [pageGuard('authoringCourses')],
        loadComponent: () =>
          import('./features/course-studio/course-studio.component').then(m => m.CourseStudioComponent),
        title: 'Authoring · Courses - TrueNorth Range',
      },
      {
        // Ranges tab is now the range list; the designer opens per range.
        path: 'ranges',
        canActivate: [pageGuard('authoringRanges')],
        loadComponent: () =>
          import('./features/ranges/ranges.component').then(m => m.RangesComponent),
        title: 'Authoring · Ranges - TrueNorth Range',
      },
      {
        path: 'ranges/designer',
        canActivate: [pageGuard('authoringRanges')],
        loadComponent: () =>
          import('./features/range-designer/range-designer.component').then(m => m.RangeDesignerComponent),
        title: 'Authoring · Range Designer - TrueNorth Range',
      },
      {
        // Greyspace (simulated internet) for one range; :rangeId binds to the panel's input.
        path: 'ranges/:rangeId/greyspace',
        canActivate: [pageGuard('authoringRanges')],
        loadComponent: () =>
          import('./features/greyspace/greyspace-panel.component').then(m => m.GreyspacePanelComponent),
        title: 'Authoring · Greyspace - TrueNorth Range',
      },
      {
        // Background noise for one range: white cell only (noise:read; the API checks
        // noise:read / noise:control on every call).
        path: 'ranges/:id/noise',
        canActivate: [pageGuard('authoringNoise')],
        loadComponent: () =>
          import('./features/noise/noise-console.component').then(m => m.NoiseConsoleComponent),
        title: 'Authoring · Background noise - TrueNorth Range',
      },
      {
        path: 'scenarios',
        canActivate: [pageGuard('authoringScenarios')],
        loadComponent: () =>
          import('./features/scenario-studio/scenario-studio.component').then(m => m.ScenarioStudioComponent),
        title: 'Authoring · Scenarios - TrueNorth Range',
      },
      {
        path: 'detections',
        canActivate: [pageGuard('authoringDetections')],
        loadComponent: () =>
          import('./features/detection-editor/detection-editor.component').then(m => m.DetectionEditorComponent),
        title: 'Authoring · Detections - TrueNorth Range',
      },
      {
        path: 'threat-intel',
        canActivate: [pageGuard('authoringThreatIntel')],
        loadComponent: () =>
          import('./features/threat-intel/threat-intel-feeds.component').then(m => m.ThreatIntelFeedsComponent),
        title: 'Authoring · Threat intel - TrueNorth Range',
      },
      {
        path: 'mesl',
        canActivate: [pageGuard('authoringExercises')],
        loadComponent: () =>
          import('./features/mesl/mesl-home.component').then(m => m.MeslHomeComponent),
        title: 'Authoring · MESL - TrueNorth Range',
      },
      {
        path: 'mesl/:id',
        canActivate: [pageGuard('authoringExercises')],
        loadComponent: () =>
          import('./features/mesl/mesl-board.component').then(m => m.MeslBoardComponent),
        title: 'Authoring · MESL Board - TrueNorth Range',
      },
      {
        path: 'forge',
        canActivate: [pageGuard('authoringExercises')],
        loadComponent: () =>
          import('./features/exercise-forge/exercise-forge.component').then(m => m.ExerciseForgeComponent),
        title: 'Authoring · Forge - TrueNorth Range',
      },
      {
        path: 'content',
        canActivate: [pageGuard('authoringContent')],
        loadComponent: () =>
          import('./features/content-catalog/content-catalog.component').then(m => m.ContentCatalogComponent),
        title: 'Authoring · Content - TrueNorth Range',
      },
    ],
  },
  // -- Learning hub (consolidates curriculum, courses, progress, competency) --
  {
    path: 'learning',
    canActivate: [authGuard, onboardingGuard],
    loadComponent: () => import('./shared/hub-shell.component').then(m => m.HubShellComponent),
    data: {
      title: 'Learning',
      // Learner-facing tabs first; the authoring and administration screens last.
      tabs: [
        { label: 'Career path', path: 'career-path' },
        { label: 'Courses', path: 'courses' },
        { label: 'My progress', path: 'progress' },
        { label: 'Competency', path: 'competency' },
        // The Curriculum Forge is authoring: the API needs course:author on every call.
        { label: 'Curriculum', path: 'curriculum', instructorOnly: true },
        // Fictional records: off unless the deployment sets TN_DEMO_PREVIEWS=true.
        { label: 'Demo previews', path: 'previews', requires: 'demo' },
        { label: 'Admin', path: 'admin', instructorOnly: true },
      ],
    },
    children: [
      { path: '', redirectTo: 'career-path', pathMatch: 'full' },
      // Old name for the career path; redirects keep ?qual= deep links working.
      { path: 'qualifications', redirectTo: 'career-path', pathMatch: 'full' },
      {
        path: 'previews',
        canActivate: [demoGuard],
        loadComponent: () =>
          import('./features/learning-preview/learning-preview.component').then(m => m.LearningPreviewComponent),
        title: 'LMS & readiness previews - TrueNorth',
      },
      {
        path: 'career-path',
        loadComponent: () =>
          import('./features/qsp-curriculum/qsp-curriculum.component').then(m => m.QspCurriculumComponent),
        title: 'Career path - TrueNorth Range',
      },
      {
        path: 'curriculum',
        canActivate: [instructorGuard],
        loadComponent: () =>
          import('./features/curriculum-forge/curriculum-forge.component').then(m => m.CurriculumForgeComponent),
        title: 'Curriculum - TrueNorth Range',
      },
      {
        // What a learner browses. Administration moved to the Admin tab.
        path: 'courses',
        loadComponent: () =>
          import('./features/training/course-catalogue.component').then(m => m.CourseCatalogueComponent),
        title: 'Courses - TrueNorth Range',
      },
      {
        path: 'admin',
        canActivate: [instructorGuard],
        loadComponent: () =>
          import('./features/training/training.component').then(m => m.TrainingComponent),
        title: 'Course administration - TrueNorth Range',
      },
      {
        // A course of its own, so the developmental path can link to one rather than
        // to the list it sits in.
        path: 'courses/:id',
        loadComponent: () =>
          import('./features/training/course-detail.component').then(m => m.CourseDetailComponent),
        title: 'Course - TrueNorth Range',
      },
      {
        path: 'progress',
        loadComponent: () =>
          import('./features/my-progress/my-progress.component').then(m => m.MyProgressComponent),
        title: 'My Progress - TrueNorth Range',
      },
      {
        path: 'competency',
        loadComponent: () =>
          import('./features/competency/competency.component').then(m => m.CompetencyComponent),
        title: 'Competency - TrueNorth Range',
      },
    ],
  },
  // -- Legacy paths kept as redirects into the hubs (no broken links) --
  { path: 'range-designer', redirectTo: 'authoring/ranges/designer', pathMatch: 'full' },
  { path: 'scenario-builder', redirectTo: 'authoring/scenarios', pathMatch: 'full' },
  { path: 'detection-editor', redirectTo: 'authoring/detections', pathMatch: 'full' },
  { path: 'exercise-forge', redirectTo: 'authoring/forge', pathMatch: 'full' },
  { path: 'content', redirectTo: 'authoring/content', pathMatch: 'full' },
  { path: 'curriculum-forge', redirectTo: 'learning/curriculum', pathMatch: 'full' },
  { path: 'training', redirectTo: 'learning/courses', pathMatch: 'full' },
  { path: 'my-progress', redirectTo: 'learning/progress', pathMatch: 'full' },
  { path: 'competency', redirectTo: 'learning/competency', pathMatch: 'full' },
  {
    path: 'exercises',
    canActivate: [authGuard, onboardingGuard],
    loadComponent: () =>
      import('./features/exercises/exercises.component').then(m => m.ExercisesComponent),
    title: 'Exercises - TrueNorth Range',
  },
  {
    // Bookings, capacity and the calendar feed (docs/adr/0004-scheduler-module.md). Staff only.
    path: 'schedule',
    canActivate: [authGuard, onboardingGuard, scheduleGuard],
    loadComponent: () =>
      import('./features/schedule/schedule.component').then(m => m.ScheduleComponent),
    title: 'Schedule - TrueNorth Range',
  },
  // The calendar (ADR 0014 §21-24): the Events view (#285) for every role on staff that reads
  // the schedule (observers read-only), and the Content view, the content clock (#286). Booking
  // a session stays on /schedule above, the booking workbench the Events view links to.
  { path: 'calendar', redirectTo: 'calendar/events', pathMatch: 'full' },
  {
    path: 'calendar/events',
    canActivate: [authGuard, onboardingGuard, pageGuard('schedule')],
    loadComponent: () =>
      import('./features/calendar/events-calendar.component').then(m => m.EventsCalendarComponent),
    title: 'Calendar: Events - TrueNorth Range',
  },
  {
    path: 'calendar/content',
    canActivate: [authGuard, onboardingGuard, pageGuard('contentCalendar')],
    loadComponent: () =>
      import('./features/calendar/content-calendar.component').then(m => m.ContentCalendarComponent),
    title: 'Calendar: Content - TrueNorth Range',
  },
  {
    // aar:read: Students (their own exercises' reports, scoped by the API), observers
    // (read only), instructors and admins.
    path: 'scoring',
    canActivate: [authGuard, onboardingGuard, pageGuard('scoring')],
    loadComponent: () =>
      import('./features/scoring/scoring.component').then(m => m.ScoringComponent),
    title: 'Scoring & AAR - TrueNorth Range',
  },
  {
    path: 'telemetry',
    canActivate: [authGuard, onboardingGuard, pageGuard('telemetry')],
    loadComponent: () =>
      import('./features/telemetry/telemetry.component').then(m => m.TelemetryComponent),
    title: 'Telemetry - TrueNorth Range',
  },
  {
    path: 'users',
    canActivate: [authGuard, adminGuard],
    loadComponent: () =>
      import('./features/users/users.component').then(m => m.UsersComponent),
    title: 'Users & Teams - TrueNorth Range',
  },
  {
    // The registration queue on its own page, for instructors (registration:read); the
    // same panel is also a tab of the admin's /users.
    path: 'approvals',
    canActivate: [authGuard, onboardingGuard, pageGuard('approvals')],
    loadComponent: () =>
      import('./features/approvals/approvals.component').then(m => m.ApprovalsComponent),
    title: 'Registration approvals - TrueNorth Range',
  },
  {
    // ADR 0014 §20 (#284): a Student's own classes, their status and Join (class:attend).
    path: 'my-classes',
    canActivate: [authGuard, onboardingGuard, pageGuard('myClasses')],
    loadComponent: () =>
      import('./features/classes/my-classes.component').then(m => m.MyClassesComponent),
    title: 'My classes - TrueNorth Range',
  },
  {
    // ADR 0014: classes and their roll call, for the staff who lead, assist or observe them.
    path: 'classes',
    canActivate: [authGuard, onboardingGuard, pageGuard('classes')],
    loadComponent: () =>
      import('./features/classes/classes.component').then(m => m.ClassesComponent),
    title: 'Classes - TrueNorth Range',
  },
  {
    // ADR 0013 §7: windows in which provision, reset and normal destroy are refused.
    path: 'maintenance-windows',
    canActivate: [authGuard, onboardingGuard, pageGuard('maintenanceWindows')],
    loadComponent: () =>
      import('./features/maintenance-windows/maintenance-windows.component').then(m => m.MaintenanceWindowsComponent),
    title: 'Maintenance windows - TrueNorth Range',
  },
  {
    // Before 'admin'. Platform administrator: drafts and publishing; any admin: the export.
    path: 'admin/rules-of-use',
    canActivate: [authGuard, onboardingGuard, adminGuard],
    loadComponent: () =>
      import('./features/acknowledgement/rules-of-use-admin.component').then(m => m.RulesOfUseAdminComponent),
    title: 'Rules of use - TrueNorth Range',
  },
  {
    path: 'admin',
    canActivate: [authGuard, adminGuard],
    loadComponent: () =>
      import('./features/admin/admin.component').then(m => m.AdminComponent),
    title: 'Admin - TrueNorth Range',
  },
  {
    path: 'quiz-player',
    canActivate: [authGuard, onboardingGuard],
    loadComponent: () =>
      import('./features/quiz-player/quiz-player.component').then(m => m.QuizPlayerComponent),
    title: 'Quiz - TrueNorth Range',
  },
  {
    path: 'integrations',
    canActivate: [authGuard, onboardingGuard, adminGuard],
    loadComponent: () =>
      import('./features/integrations/integrations.component').then(m => m.IntegrationsComponent),
    title: 'Integrations - TrueNorth Range',
  },
  // -- Infrastructure & AI --
  {
    path: 'infrastructure',
    canActivate: [authGuard, onboardingGuard, pageGuard('infrastructure')],
    loadComponent: () =>
      import('./features/infrastructure/infrastructure.component').then(m => m.InfrastructureComponent),
    title: 'Infrastructure - TrueNorth Range',
  },
  {
    path: 'ai-orchestrator',
    canActivate: [authGuard, onboardingGuard, pageGuard('aiOrchestrator')],
    loadComponent: () =>
      import('./features/ai-orchestrator/ai-orchestrator.component').then(m => m.AiOrchestratorComponent),
    title: 'AI Orchestrator - TrueNorth Range',
  },
  // -- Knowledge base + trouble tickets --
  {
    path: 'wiki',
    canActivate: [authGuard, onboardingGuard],
    loadChildren: () => import('./features/wiki/wiki.routes').then(m => m.WIKI_ROUTES),
  },
  {
    path: 'support',
    canActivate: [authGuard, onboardingGuard, pageGuard('support')],
    loadChildren: () => import('./features/tickets/tickets.routes').then(m => m.SUPPORT_ROUTES),
  },
  { path: 'tickets', redirectTo: 'support', pathMatch: 'full' },
  {
    path: 'login',
    loadComponent: () =>
      import('./features/login/login.component').then(m => m.LoginComponent),
    title: 'Login - TrueNorth Range',
  },
  {
    path: 'topology-3d',
    canActivate: [authGuard, onboardingGuard],
    loadComponent: () =>
      import('./features/ops-center/topology-3d.component').then(m => m.Topology3dComponent),
    title: '3D Topology - TrueNorth Range',
  },
  {
    path: 'exercises/:id',
    // Students run their assessment and submit detections here (ADR 0005); the API
    // redacts the answer key for them and gates every action by permission.
    canActivate: [authGuard, onboardingGuard],
    loadComponent: () =>
      import('./features/exercise-detail/exercise-detail.component').then(m => m.ExerciseDetailComponent),
    title: 'Exercise - TrueNorth Range',
  },
  {
    // exercise:read + telemetry:read: instructors and admins run it, observers and range
    // ops watch it (no mutating controls without exercise:create). Not Students.
    path: 'ops-center/select',
    canActivate: [authGuard, onboardingGuard, pageGuard('opsCenter')],
    loadComponent: () =>
      import('./features/ops-center/exercise-selector.component').then(m => m.ExerciseSelectorComponent),
    title: 'Select an exercise - TrueNorth',
  },
  {
    path: 'ops-center/:exerciseId',
    canActivate: [authGuard, onboardingGuard, pageGuard('opsCenter')],
    loadComponent: () =>
      import('./features/ops-center/ops-center.component').then(m => m.OpsCenterComponent),
    title: 'Ops Center - TrueNorth Range',
  },
  {
    // An LMS launch of a Student with no TrueNorth sign-in: exchanges the code in the
    // fragment for a TrueNorth session, then goes on to what was launched. No guard: this
    // page is the sign-in (control-plane/api/app/lti_identity/session.py).
    path: 'lti/session',
    loadComponent: () =>
      import('./features/lti-session/lti-session.component').then(m => m.LtiSessionComponent),
    title: 'Opening your activity - TrueNorth Range',
  },
  {
    // Staff deep linking: confirm, signed in as yourself, that an LMS account is yours
    // (control-plane/api/app/lti_identity/links.py). The page sends you to sign in first.
    path: 'lti/link',
    loadComponent: () =>
      import('./features/lti-session/lti-link.component').then(m => m.LtiLinkComponent),
    title: 'Link a learning-platform account - TrueNorth Range',
  },
  {
    // cmi5 (docs/cmi5.md): a released course's assignable units, launched with TrueNorth as
    // the LMS. An AU's returnURL brings the Student back here.
    path: 'au/releases/:releaseId',
    canActivate: [authGuard],
    loadComponent: () => import('./features/cmi5/au-launcher.component').then(m => m.Cmi5LauncherComponent),
    title: 'Course modules - TrueNorth Range',
  },
  {
    // One AU, launched by an LMS (TrueNorth, Moodle, PCTE) with the cmi5 launch parameters
    // in the query. authGuard keeps them through sign-in: returnUrl carries the query.
    path: 'au/releases/:releaseId/:auIndex',
    canActivate: [authGuard],
    loadComponent: () => import('./features/cmi5/au-runtime.component').then(m => m.Cmi5AuRuntimeComponent),
    title: 'Module - TrueNorth Range',
  },
  {
    // A student's lab. No guard: a Moodle launch arrives with a token for this one lab
    // (URL fragment); without one the page itself asks the student to sign in.
    path: 'labs/:id',
    loadComponent: () => import('./features/labs/lab-page.component').then(m => m.LabPageComponent),
    title: 'Lab - TrueNorth Range',
  },
  // -- R0-B operator screens (range recovery, team membership, Student progress). Kept
  //    in one block so menu changes elsewhere do not collide with it. The API checks every
  //    call; these guards (PAGE_ACCESS, as the menu reads it) only keep each screen to the
  //    roles that can use it. --
  {
    path: 'ranges/:id',
    canActivate: [authGuard, onboardingGuard, pageGuard('rangeRecovery')],
    loadComponent: () => import('./features/ranges/range-detail.component').then(m => m.RangeDetailComponent),
    title: 'Range recovery - TrueNorth Range',
  },
  {
    path: 'teams',
    canActivate: [authGuard, onboardingGuard, pageGuard('teams')],
    loadComponent: () => import('./features/teams/team-members.component').then(m => m.TeamMembersComponent),
    title: 'Team membership - TrueNorth Range',
  },
  {
    // Under the course administration area; the 'learning' hub has no such child, so the
    // router falls through to this route.
    path: 'learning/admin/progress',
    canActivate: [authGuard, onboardingGuard, pageGuard('learnerProgress')],
    loadComponent: () =>
      import('./features/training/learner-progress.component').then(m => m.LearnerProgressComponent),
    title: 'Student progress - TrueNorth Range',
  },
  {
    path: '**',
    redirectTo: 'dashboard',
  },
];
