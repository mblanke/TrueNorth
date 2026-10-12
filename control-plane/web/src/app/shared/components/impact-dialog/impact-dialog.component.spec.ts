import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { ImpactDialogComponent, ImpactDialogData } from './impact-dialog.component';

describe('ImpactDialogComponent', () => {
  let close: jasmine.Spy;

  function create(data: ImpactDialogData) {
    close = jasmine.createSpy('close');
    TestBed.configureTestingModule({
      imports: [ImpactDialogComponent, NoopAnimationsModule],
      providers: [
        { provide: MAT_DIALOG_DATA, useValue: data },
        { provide: MatDialogRef, useValue: { close } },
      ],
    });
    const fixture = TestBed.createComponent(ImpactDialogComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('lists the impact before the operator confirms', () => {
    const f = create({ title: 'Restore?', impact: ['State since then is lost.'], confirmText: 'Restore' });
    expect(f.nativeElement.textContent).toContain('State since then is lost.');
    f.componentInstance.confirm();
    expect(close).toHaveBeenCalledWith({ reason: '' });
  });

  it('holds the confirm until the reason is long enough and the name is typed', () => {
    const f = create({ title: 'Abandon?', impact: ['x'], confirmText: 'Abandon', reasonLabel: 'Reason', reasonMin: 10,
      typeToConfirm: 'blue-range' });
    const c = f.componentInstance;
    const confirmBtn = () => Array.from(f.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>)
      .find(b => b.textContent?.includes('Abandon'))!;
    expect(confirmBtn().disabled).toBeTrue();
    c.reason = 'too short';
    c.typed = 'blue-range';
    f.detectChanges();
    expect(c.ready()).toBeFalse();
    c.confirm();
    expect(close).not.toHaveBeenCalled();
    c.reason = 'vCenter checked, nothing running';
    f.detectChanges();
    expect(confirmBtn().disabled).toBeFalse();
    c.confirm();
    expect(close).toHaveBeenCalledWith({ reason: 'vCenter checked, nothing running' });
  });
});
