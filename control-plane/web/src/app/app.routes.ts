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
  {
    path: 'ranges',
    loadComponent: () =>
      import('./features/ranges/ranges.component').then(m => m.RangesComponent),
    title: 'Ranges - TrueNorth Range',
  },
  {
    path: 'templates',
    loadComponent: () =>
      import('./features/templates/templates.component').then(m => m.TemplatesComponent),
    title: 'Templates - TrueNorth Range',
  },
  {
    path: 'range-designer',
    loadComponent: () =>
      import('./features/range-designer/range-designer.component').then(m => m.RangeDesignerComponent),
    title: 'Range Designer - TrueNorth Range',
  },
  {
    path: 'scenarios',
    loadComponent: () =>
      import('./features/scenarios/scenarios.component').then(m => m.ScenariosComponent),
    title: 'Scenarios - TrueNorth Range',
  },
  {
    path: 'scenario-builder',
    loadComponent: () =>
      import('./features/scenario-builder/scenario-builder.component').then(m => m.ScenarioBuilderComponent),
    title: 'Scenario Builder - TrueNorth Range',
  },
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
    path: 'content',
    loadComponent: () =>
      import('./features/content-catalog/content-catalog.component').then(m => m.ContentCatalogComponent),
    title: 'Content Catalog - TrueNorth Range',
  },
  // -- LMS & Training --
  {
    path: 'training',
    loadComponent: () =>
      import('./features/training/training.component').then(m => m.TrainingComponent),
    title: 'Training Portal - TrueNorth Range',
  },
  {
    path: 'my-progress',
    loadComponent: () =>
      import('./features/my-progress/my-progress.component').then(m => m.MyProgressComponent),
    title: 'My Progress - TrueNorth Range',
  },
  {
    path: 'competency',
    loadComponent: () =>
      import('./features/competency/competency.component').then(m => m.CompetencyComponent),
    title: 'Competency Framework - TrueNorth Range',
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
    path: 'detection-editor',
    loadComponent: () =>
      import('./features/detection-editor/detection-editor.component').then(m => m.DetectionEditorComponent),
    title: 'Detection Rule Editor - TrueNorth Range',
  },
  {
    path: 'exercise-forge',
    loadComponent: () =>
      import('./features/exercise-forge/exercise-forge.component').then(m => m.ExerciseForgeComponent),
    title: 'Exercise Forge - TrueNorth Range',
  },
  {
    path: 'topology-3d',
    loadComponent: () =>
      import('./features/ops-center/topology-3d.component').then(m => m.Topology3dComponent),
    title: '3D Topology - TrueNorth Range',
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