import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    redirectTo: 'dashboard',
    pathMatch: 'full',
  },
  {
    path: 'dashboard',
    loadComponent: () =>
      import('./features/dashboard/dashboard.component').then(m => m.DashboardComponent),
    title: 'Dashboard - TrueNorth Range',
  },
  // Stray top-level list pages folded into the Authoring Studio hub.
  { path: 'ranges', redirectTo: 'authoring/ranges', pathMatch: 'full' },
  { path: 'templates', redirectTo: 'authoring/content', pathMatch: 'full' },
  { path: 'scenarios', redirectTo: 'authoring/scenarios', pathMatch: 'full' },
  // -- Authoring Studio hub (consolidates the design/authoring screens) --
  {
    path: 'authoring',
    loadComponent: () => import('./shared/hub-shell.component').then(m => m.HubShellComponent),
    data: {
      title: 'Authoring Studio',
      tabs: [
        { label: 'Ranges', path: 'ranges' },
        { label: 'Scenarios', path: 'scenarios' },
        { label: 'Detections', path: 'detections' },
        { label: 'MESL', path: 'mesl' },
        { label: 'Forge', path: 'forge' },
        { label: 'Content', path: 'content' },
      ],
    },
    children: [
      { path: '', redirectTo: 'ranges', pathMatch: 'full' },
      {
        // Ranges tab is now the range list; the designer opens per range.
        path: 'ranges',
        loadComponent: () =>
          import('./features/ranges/ranges.component').then(m => m.RangesComponent),
        title: 'Authoring · Ranges - TrueNorth Range',
      },
      {
        path: 'ranges/designer',
        loadComponent: () =>
          import('./features/range-designer/range-designer.component').then(m => m.RangeDesignerComponent),
        title: 'Authoring · Range Designer - TrueNorth Range',
      },
      {
        path: 'scenarios',
        loadComponent: () =>
          import('./features/scenario-studio/scenario-studio.component').then(m => m.ScenarioStudioComponent),
        title: 'Authoring · Scenarios - TrueNorth Range',
      },
      {
        path: 'detections',
        loadComponent: () =>
          import('./features/detection-editor/detection-editor.component').then(m => m.DetectionEditorComponent),
        title: 'Authoring · Detections - TrueNorth Range',
      },
      {
        path: 'mesl',
        loadComponent: () =>
          import('./features/mesl/mesl-home.component').then(m => m.MeslHomeComponent),
        title: 'Authoring · MESL - TrueNorth Range',
      },
      {
        path: 'mesl/:id',
        loadComponent: () =>
          import('./features/mesl/mesl-board.component').then(m => m.MeslBoardComponent),
        title: 'Authoring · MESL Board - TrueNorth Range',
      },
      {
        path: 'forge',
        loadComponent: () =>
          import('./features/exercise-forge/exercise-forge.component').then(m => m.ExerciseForgeComponent),
        title: 'Authoring · Forge - TrueNorth Range',
      },
      {
        path: 'content',
        loadComponent: () =>
          import('./features/content-catalog/content-catalog.component').then(m => m.ContentCatalogComponent),
        title: 'Authoring · Content - TrueNorth Range',
      },
    ],
  },
  // -- Learning hub (consolidates curriculum, courses, progress, competency) --
  {
    path: 'learning',
    loadComponent: () => import('./shared/hub-shell.component').then(m => m.HubShellComponent),
    data: {
      title: 'Learning',
      tabs: [
        { label: 'Qualifications', path: 'qualifications' },
        { label: 'Curriculum', path: 'curriculum' },
        { label: 'Courses', path: 'courses' },
        { label: 'My Progress', path: 'progress' },
        { label: 'Competency', path: 'competency' },
      ],
    },
    children: [
      { path: '', redirectTo: 'qualifications', pathMatch: 'full' },
      {
        path: 'qualifications',
        loadComponent: () =>
          import('./features/qsp-curriculum/qsp-curriculum.component').then(m => m.QspCurriculumComponent),
        title: 'QSP Curriculum - TrueNorth Range',
      },
      {
        path: 'curriculum',
        loadComponent: () =>
          import('./features/curriculum-forge/curriculum-forge.component').then(m => m.CurriculumForgeComponent),
        title: 'Curriculum - TrueNorth Range',
      },
      {
        path: 'courses',
        loadComponent: () =>
          import('./features/training/training.component').then(m => m.TrainingComponent),
        title: 'Courses - TrueNorth Range',
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
    loadComponent: () =>
      import('./features/exercises/exercises.component').then(m => m.ExercisesComponent),
    title: 'Exercises - TrueNorth Range',
  },
  {
    path: 'scoring',
    loadComponent: () =>
      import('./features/scoring/scoring.component').then(m => m.ScoringComponent),
    title: 'Scoring & AAR - TrueNorth Range',
  },
  {
    path: 'telemetry',
    loadComponent: () =>
      import('./features/telemetry/telemetry.component').then(m => m.TelemetryComponent),
    title: 'Telemetry - TrueNorth Range',
  },
  {
    path: 'users',
    loadComponent: () =>
      import('./features/users/users.component').then(m => m.UsersComponent),
    title: 'Users & Teams - TrueNorth Range',
  },
  {
    path: 'admin',
    loadComponent: () =>
      import('./features/admin/admin.component').then(m => m.AdminComponent),
    title: 'Admin - TrueNorth Range',
  },
  {
    path: 'quiz-player',
    loadComponent: () =>
      import('./features/quiz-player/quiz-player.component').then(m => m.QuizPlayerComponent),
    title: 'Quiz - TrueNorth Range',
  },
  {
    path: 'integrations',
    loadComponent: () =>
      import('./features/integrations/integrations.component').then(m => m.IntegrationsComponent),
    title: 'Integrations - TrueNorth Range',
  },
  // -- Infrastructure & AI --
  {
    path: 'infrastructure',
    loadComponent: () =>
      import('./features/infrastructure/infrastructure.component').then(m => m.InfrastructureComponent),
    title: 'Infrastructure - TrueNorth Range',
  },
  {
    path: 'ai-orchestrator',
    loadComponent: () =>
      import('./features/ai-orchestrator/ai-orchestrator.component').then(m => m.AiOrchestratorComponent),
    title: 'AI Orchestrator - TrueNorth Range',
  },
  {
    path: 'login',
    loadComponent: () =>
      import('./features/login/login.component').then(m => m.LoginComponent),
    title: 'Login - TrueNorth Range',
  },
  {
    path: 'topology-3d',
    loadComponent: () =>
      import('./features/ops-center/topology-3d.component').then(m => m.Topology3dComponent),
    title: '3D Topology - TrueNorth Range',
  },
  {
    path: 'exercises/:id',
    loadComponent: () =>
      import('./features/exercise-detail/exercise-detail.component').then(m => m.ExerciseDetailComponent),
    title: 'Exercise - TrueNorth Range',
  },
  {
    path: 'ops-center/:exerciseId',
    loadComponent: () =>
      import('./features/ops-center/ops-center.component').then(m => m.OpsCenterComponent),
    title: 'Ops Center - TrueNorth Range',
  },
  {
    path: '**',
    redirectTo: 'dashboard',
  },
];