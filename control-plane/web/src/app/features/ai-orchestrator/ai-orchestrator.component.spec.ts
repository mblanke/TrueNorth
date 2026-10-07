import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import { AiOrchestratorComponent } from './ai-orchestrator.component';
import { AiConfigApiService } from '@core/services/ai-config-api.service';

describe('AiOrchestratorComponent', () => {
  let component: AiOrchestratorComponent;
  let fixture: ComponentFixture<AiOrchestratorComponent>;
  let api: jasmine.SpyObj<AiConfigApiService>;
  let snack: jasmine.SpyObj<MatSnackBar>;

  const primary = {
    id: 'b-1', name: 'LiteLLM', backend_type: 'litellm', base_url: 'http://litellm:4000/v1',
    is_active: true, is_primary: true, max_concurrent: 10, timeout_seconds: 120,
  };

  beforeEach(async () => {
    api = jasmine.createSpyObj('AiConfigApiService',
      ['backends', 'routes', 'summary', 'models', 'testGenerate', 'discoverFleet']);
    snack = jasmine.createSpyObj('MatSnackBar', ['open']);
    api.backends.and.returnValue(of([primary] as any));
    api.routes.and.returnValue(of([]));
    api.summary.and.returnValue(of({
      total_backends: 1, active_backends: 1, total_nodes: 0, online_nodes: 0,
      total_gpu_vram_gb: 0, active_requests: 0, model_routes: 0, by_backend_type: { litellm: 1 },
    } as any));
    api.models.and.returnValue(of({ models: [{ name: 'agent', node: 'r7725' }], count: 1 } as any));

    await TestBed.configureTestingModule({
      imports: [AiOrchestratorComponent, NoopAnimationsModule],
      providers: [
        { provide: AiConfigApiService, useValue: api },
        { provide: MatSnackBar, useValue: snack },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(AiOrchestratorComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('loads backends, routes, summary and models on init', () => {
    fixture.detectChanges();
    expect(component.backends.length).toBe(1);
    expect(component.summary?.total_backends).toBe(1);
    expect(component.availableModels.map(m => m.name)).toEqual(['agent']);
  });

  it('testGenerate sends prompt and model and shows the result', () => {
    const reply = { response: 'pong', model: 'agent', backend: 'LiteLLM', latency_ms: 12 };
    api.testGenerate.and.returnValue(of(reply));
    component.testPrompt = 'ping';
    component.selectedModel = 'agent';

    component.testGenerate();

    expect(api.testGenerate).toHaveBeenCalledWith('ping', 'agent');
    expect(component.testResult).toEqual(reply);
    expect(component.generating).toBeFalse();
  });

  it('testGenerate surfaces a failure and stops spinning', () => {
    api.testGenerate.and.returnValue(throwError(() => ({ error: { detail: 'orchestrator down' } })));

    component.testGenerate();

    expect(component.testResult).toBeNull();
    expect(component.generating).toBeFalse();
    expect(snack.open.calls.mostRecent().args[0]).toContain('orchestrator down');
  });

  it('scanFleet refuses without a primary backend', () => {
    component.backends = [];
    component.scanFleet();
    expect(api.discoverFleet).not.toHaveBeenCalled();
    expect(snack.open).toHaveBeenCalled();
  });
});
