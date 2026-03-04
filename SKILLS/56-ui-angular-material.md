# Angular + Material M3 Design System
## Standard choice
- Preferred UI framework: Angular 17+ with Angular Material (M3 theme).
- One design system per repo.
## Setup
- Install: @angular/material @angular/cdk
- Use Angular Material M3 theming (define-theme with color, typography, density)
## Theming rules
- Define a single theme and reuse everywhere.
- Use semantic colors (primary/secondary/error/warning).
- Prefer Angular Material components over custom HTML/CSS.
## Layout conventions
- Use: mat-toolbar + mat-sidenav + router-outlet
- Keep pages as composition: Page -> Sections -> Widgets
- Forms: mat-form-field + mat-error for validation
## Accessibility
- All inputs have mat-label
- Keyboard navigation works
- Focus trapped in dialogs
## Performance
- Lazy-load route modules
- OnPush change detection for presentational components
- Virtual scroll for large lists (cdk-virtual-scroll-viewport)
