import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { NotificationService } from '@core/services/notification.service';
import { RangeRecoveryApiService, RangeSnapshot } from '@core/services/range-recovery-api.service';
import { RangeSnapshotsComponent } from './range-snapshots.component';

const snap = (over: Partial<RangeSnapshot>): RangeSnapshot => ({
  id: 's1', range_id: 'r1', name: 'baseline', description: null, range_state_at_snapshot: 'ready',
  snapshot_state: 'ready', size_bytes: 0, tenant_id: 't', created_at: '2026-10-10T10:00:00Z',
  updated_at: '2026-10-10T10:00:00Z', ...over,
});

describe('RangeSnapshotsComponent', () => {
  let fixture: ComponentFixture<RangeSnapshotsComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<RangeRecoveryApiService>;
  let dialogOpen: jasmine.Spy;
  let confirmed: boolean;

  async function render(list: RangeSnapshot[] | 'error', inputs: Record<string, unknown> = {}) {
    api = jasmine.createSpyObj('RangeRecoveryApiService', ['listSnapshots', 'createSnapshot', 'deleteSnapshot',
      'restoreSnapshot']);
    api.listSnapshots.and.returnValue(list === 'error'
      ? throwError(() => new HttpErrorResponse({ status: 404, error: { detail: 'Range not found' } }))
      : of(list));
    confirmed = true;
    dialogOpen = jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of(confirmed ? { reason: '' } : null) }));
    await TestBed.configureTestingModule({
      imports: [RangeSnapshotsComponent, NoopAnimationsModule],
      providers: [
        { provide: RangeRecoveryApiService, useValue: api },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RangeSnapshotsComponent);
    fixture.componentRef.setInput('rangeId', 'r1');
    fixture.componentRef.setInput('rangeState', inputs['rangeState'] ?? 'ready');
    fixture.componentRef.setInput('canProvision', inputs['canProvision'] ?? true);
    fixture.componentRef.setInput('canDelete', inputs['canDelete'] ?? true);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const button = (text: string) =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes(text)) as HTMLButtonElement | undefined;

  it('shows the empty state and the load error distinctly', async () => {
    await render([]);
    expect(el.textContent).toContain('No snapshots of this range.');
    TestBed.resetTestingModule();
    await render('error');
    expect(el.textContent).toContain('Range not found');
    expect(el.textContent).not.toContain('No snapshots');
  });

  it('takes a snapshot and reports the accepted, not finished, state', async () => {
    await render([]);
    api.createSnapshot.and.returnValue(of(snap({ snapshot_state: 'creating' })));
    const comp = fixture.componentInstance;
    comp.name = 'before-ex';
    comp.create();
    fixture.detectChanges();
    expect(api.createSnapshot).toHaveBeenCalledWith('r1', 'before-ex', '');
    expect(el.textContent).toContain('accepted: creating');
    expect(api.listSnapshots).toHaveBeenCalledTimes(2);
  });

  it('cannot snapshot a range in the wrong state', async () => {
    await render([], { rangeState: 'provisioning' });
    expect(el.textContent).toContain('this one is provisioning');
    expect(button('Take snapshot')!.disabled).toBeTrue();
  });

  it('restore warns of state loss before calling the API', async () => {
    await render([snap({})]);
    api.restoreSnapshot.and.returnValue(of({} as any));
    const changed = jasmine.createSpy('changed');
    fixture.componentInstance.changed.subscribe(changed);
    button('Restore')!.click();
    const impact = dialogOpen.calls.mostRecent().args[1].data.impact.join(' ');
    expect(impact).toContain('Everything since then is lost');
    expect(api.restoreSnapshot).toHaveBeenCalledWith('r1', 's1');
    expect(changed).toHaveBeenCalled();
  });

  it('a declined confirmation deletes nothing', async () => {
    await render([snap({})]);
    confirmed = false;
    button('Delete')!.click();
    expect(api.deleteSnapshot).not.toHaveBeenCalled();
  });

  it('shows a 409 refusal from the API', async () => {
    await render([snap({})]);
    api.deleteSnapshot.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 409, error: { detail: 'Snapshot is restoring; delete it once that finishes' },
    })));
    button('Delete')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Refused: Snapshot is restoring');
  });

  it('offers no restore or delete while a snapshot is busy, nor to read-only users', async () => {
    await render([snap({ snapshot_state: 'restoring' })]);
    expect(button('Restore')).toBeUndefined();
    expect(button('Delete')).toBeUndefined();
    TestBed.resetTestingModule();
    await render([snap({})], { canProvision: false, canDelete: false });
    expect(button('Restore')).toBeUndefined();
    expect(button('Delete')).toBeUndefined();
    expect(el.querySelector('form')).toBeNull();
  });
});
