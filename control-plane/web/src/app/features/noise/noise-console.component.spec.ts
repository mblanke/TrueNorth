import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import {
  NoiseActivity, NoiseAgent, NoiseApiService, NoiseDeploy, NoisePersona, NoiseProfile, NoiseStats,
} from '@core/services/noise-api.service';
import { Range } from '@core/models';
import { NoiseConsoleComponent, levelLabel } from './noise-console.component';

const RANGE_ID = 'r-1';

function profile(over: Partial<NoiseProfile> = {}): NoiseProfile {
  return {
    range_id: RANGE_ID, configured: true, enabled: true, paused: false, level: 40, effective_level: 40,
    seed: 7, utc_offset: 0, overrides: {}, targets: {}, pack: 'builtin',
    dial: { active_fraction: 0.49, actions_per_hour: 7.8, diurnal_amplitude: 0.24, lookalike_share: 0.038 },
    ...over,
  };
}

const AGENTS: NoiseAgent[] = [
  { id: 'a1', node: 'lnx01', zone: 'corporate_lan', version: '0.1', state: 'ok', last_seen_at: '2026-10-07T14:02:51Z' },
  { id: 'a2', node: 'tgen01', zone: 'corporate_lan', version: '', state: 'lost', last_seen_at: null },
];
const PERSONAS: NoisePersona[] = [
  { id: 'p1', handle: 'lfraser', display_name: 'Lane Fraser', title: 'Operations Officer', department: 'Operations',
    node: 'lnx01', work_start: 8, work_end: 17, habits: {}, lookalikes: false, attrs: {} },
  { id: 'p2', handle: 'sokafor', display_name: 'Sam Okafor', title: 'Systems Administrator', department: 'IT',
    node: 'lnx01', work_start: 7, work_end: 16, habits: {}, lookalikes: true, attrs: {} },
];
const ACTIVITY: NoiseActivity[] = [
  { at: '2026-10-07T14:02:51Z', node: 'lnx01', persona: 'lfraser', kind: 'ad_logon', target: 'dc01',
    lookalike: false, ok: true, detail: {} },
  { at: '2026-10-07T14:02:31Z', node: 'lnx01', persona: 'sokafor', kind: 'admin_scan', target: '10.10.0.0/24',
    lookalike: true, ok: false, detail: {} },
];
const STATS: NoiseStats = {
  minutes: 60, total: 4, failed: 1, lookalikes: 1, by_kind: { ad_logon: 3, admin_scan: 1 }, agents: { ok: 1, lost: 1 },
};
const PREVIEW: NoiseDeploy = {
  dry_run: true, agents: [{ node: 'lnx01', zone: 'corporate_lan', platform: 'linux', ip: '10.10.0.50', mgmt_ip: '10.255.0.14' }],
  skipped: [{ node: 'ws01', reason: 'windows agent not available yet' }], targets: { web: ['web01'] },
  dropped_targets: [], controller_url: 'https://10.255.0.1/api', mgmt_cidr: '10.255.0.0/24',
};

describe('NoiseConsoleComponent', () => {
  let fixture: ComponentFixture<NoiseConsoleComponent>;
  let component: NoiseConsoleComponent;
  let noise: jasmine.SpyObj<NoiseApiService>;
  let api: jasmine.SpyObj<ApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  async function setup(initial: NoiseProfile | HttpErrorResponse) {
    noise = jasmine.createSpyObj('NoiseApiService', [
      'presets', 'profile', 'update', 'agents', 'personas', 'activity', 'stats', 'deploy', 'plan',
    ]);
    api = jasmine.createSpyObj('ApiService', ['getRange']);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    api.getRange.and.returnValue(of({ id: RANGE_ID, name: 'Team Bravo range' } as Range));
    noise.presets.and.returnValue(of({
      presets: { quiet: 10, office: 40, busy: 70, chaos: 95 }, activities: [], lookalikes: [], target_pools: [],
    }));
    noise.profile.and.returnValue(initial instanceof HttpErrorResponse ? throwError(() => initial) : of(initial));
    noise.agents.and.returnValue(of(AGENTS));
    noise.personas.and.returnValue(of(PERSONAS));
    noise.activity.and.returnValue(of(ACTIVITY));
    noise.stats.and.returnValue(of(STATS));
    noise.deploy.and.callFake((_id: string, dry: boolean) => of(dry ? PREVIEW : { ...PREVIEW, dry_run: false, task_id: 't-1' }));

    await TestBed.configureTestingModule({
      imports: [NoiseConsoleComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: NoiseApiService, useValue: noise },
        { provide: ApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: RANGE_ID }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(NoiseConsoleComponent);
    component = fixture.componentInstance;
  }

  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';
  const q = (sel: string) => (fixture.nativeElement as HTMLElement).querySelector(sel);
  const buttons = () => Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button'));
  const button = (label: string) => buttons().find(b => (b.textContent ?? '').trim().startsWith(label));

  it('labels levels the way the mockup does', () => {
    expect(levelLabel(0)).toBe('silent');
    expect(levelLabel(15)).toBe('quiet');
    expect(levelLabel(40)).toBe('an ordinary office day');
    expect(levelLabel(70)).toBe('busy');
    expect(levelLabel(95)).toBe('chaos');
  });

  describe('a range without noise', () => {
    beforeEach(async () => setup(profile({ configured: false, enabled: false })));

    it('shows the setup and what a deploy would do, without polling', () => {
      fixture.detectChanges();
      expect(q('[data-testid="noise-setup"]')).not.toBeNull();
      expect(noise.deploy).toHaveBeenCalledWith(RANGE_ID, true);
      expect(text()).toContain('lnx01');
      expect(text()).toContain('mgmt 10.255.0.14');
      expect(text()).toContain('ws01 (windows agent not available yet)');
      expect(noise.agents).not.toHaveBeenCalled();
    });

    it('deploys for real and switches to the live view', fakeAsync(() => {
      fixture.detectChanges();
      noise.profile.and.returnValue(of(profile()));
      button('Deploy noise')!.click();
      tick();
      fixture.detectChanges();
      expect(noise.deploy).toHaveBeenCalledWith(RANGE_ID, false);
      expect(notify.success).toHaveBeenCalled();
      expect(q('[data-testid="noise-hero"]')).not.toBeNull();
      expect(noise.agents).toHaveBeenCalledWith(RANGE_ID);
      discardPeriodicTasks();
    }));

    it('reports a refused deploy and stays on setup', () => {
      fixture.detectChanges();
      noise.deploy.and.returnValue(throwError(() => new HttpErrorResponse({
        status: 409, error: { detail: 'range is provisioning; deploy noise once it is ready' },
      })));
      button('Deploy noise')!.click();
      fixture.detectChanges();
      expect(notify.error).toHaveBeenCalledWith('range is provisioning; deploy noise once it is ready');
      expect(q('[data-testid="noise-setup"]')).not.toBeNull();
    });

    it('explains a preview that the API refuses', () => {
      noise.deploy.and.returnValue(throwError(() => new HttpErrorResponse({
        status: 409, error: { detail: 'the range template has no `noise:` block with `enabled: true`' },
      })));
      fixture.detectChanges();
      expect(text()).toContain('no `noise:` block');
      expect(button('Deploy noise')!.disabled).toBeTrue();
    });
  });

  describe('a running range', () => {
    beforeEach(async () => setup(profile()));

    it('shows the dial, agents, the mix and the ground truth', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      fixture.detectChanges();
      expect(text()).toContain('Noise level 40 — an ordinary office day');
      expect(text()).toContain('Team Bravo range');
      expect(fixture.nativeElement.querySelectorAll('[data-testid="agent-row"]').length).toBe(2);
      expect(text()).toContain('Not reporting');
      expect(text()).toContain('Logon');
      expect(text()).toContain('75%');
      const rows = fixture.nativeElement.querySelectorAll('[data-testid="ground-truth"] tbody tr');
      expect(rows.length).toBe(2);
      expect(text()).toContain('Lookalike');
      expect(text()).toContain('Failed');
      discardPeriodicTasks();
    }));

    it('sets the level only when it was changed', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      expect(button('Set level to')).toBeUndefined();
      noise.update.and.returnValue(of(profile({ level: 70, effective_level: 70 })));
      component.level.set(70);
      fixture.detectChanges();
      button('Set level to 70')!.click();
      expect(noise.update).toHaveBeenCalledWith(RANGE_ID, { level: 70 });
      fixture.detectChanges();
      expect(text()).toContain('Noise level 70 — busy');
      discardPeriodicTasks();
    }));

    it('a preset moves the dial', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      fixture.detectChanges();
      button('Chaos')!.click();
      expect(component.level()).toBe(95);
      discardPeriodicTasks();
    }));

    it('pauses and resumes', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      noise.update.and.returnValue(of(profile({ paused: true, effective_level: 0 })));
      fixture.detectChanges();
      button('Pause all noise')!.click();
      expect(noise.update).toHaveBeenCalledWith(RANGE_ID, { paused: true });
      fixture.detectChanges();
      expect(text()).toContain('All noise is paused.');
      expect(button('Resume noise')).toBeDefined();
      discardPeriodicTasks();
    }));

    it('stops: polling ends and the setup comes back', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      noise.update.and.returnValue(of(profile({ enabled: false })));
      fixture.detectChanges();
      button('Stop noise')!.click();
      expect(noise.update).toHaveBeenCalledWith(RANGE_ID, { enabled: false });
      fixture.detectChanges();
      expect(q('[data-testid="noise-setup"]')).not.toBeNull();
      expect(text()).toContain('Background noise is stopped.');
      const polls = noise.agents.calls.count();
      tick(30000);
      expect(noise.agents.calls.count()).toBe(polls);
    }));

    it('filters the ground truth to lookalikes', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      component.setFilter('lookalikes');
      expect(noise.activity).toHaveBeenCalledWith(RANGE_ID, { limit: 100, lookalike: true });
      discardPeriodicTasks();
    }));

    it('lists and searches personas', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      component.tab.set('personas');
      component.search.set('admin');
      fixture.detectChanges();
      const rows = fixture.nativeElement.querySelectorAll('[data-testid="personas"] tbody tr');
      expect(rows.length).toBe(1);
      expect(text()).toContain('Sam Okafor');
      expect(text()).toContain('07:00–16:00');
      discardPeriodicTasks();
    }));

    it('reports a failed change and keeps the old settings', fakeAsync(() => {
      fixture.detectChanges();
      tick();
      noise.update.and.returnValue(throwError(() => new HttpErrorResponse({ status: 403, error: { detail: 'Forbidden' } })));
      component.togglePause();
      expect(notify.error).toHaveBeenCalledWith('Forbidden');
      expect(component.profile()!.paused).toBeFalse();
      discardPeriodicTasks();
    }));
  });

  describe('when the API refuses the page', () => {
    it('says so for a user without noise access', async () => {
      await setup(new HttpErrorResponse({ status: 403 }));
      fixture.detectChanges();
      expect(text()).toContain('You do not have access to background noise.');
      expect(noise.agents).not.toHaveBeenCalled();
    });

    it('says so for a range that is not the caller\'s', async () => {
      await setup(new HttpErrorResponse({ status: 404 }));
      fixture.detectChanges();
      expect(text()).toContain('Range not found.');
    });
  });
});
