// Author: Marc-Andre Blanke
// Created: 2026-11-23T16:00:00Z
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PromoteBuildDialogComponent } from './promote-build-dialog.component';

describe('PromoteBuildDialogComponent (reason required, #527)', () => {
  let fixture: ComponentFixture<PromoteBuildDialogComponent>;
  let ref: { close: jasmine.Spy };
  const q = (id: string) => (fixture.nativeElement as HTMLElement).querySelector(`[data-testid="${id}"]`) as HTMLElement;

  async function type(text: string): Promise<void> {
    const box = q('gi-reason') as HTMLTextAreaElement;
    box.value = text;
    box.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    ref = { close: jasmine.createSpy('close') };
    TestBed.configureTestingModule({
      imports: [PromoteBuildDialogComponent, NoopAnimationsModule],
      providers: [
        { provide: MatDialogRef, useValue: ref },
        { provide: MAT_DIALOG_DATA, useValue: { imageName: 'ubuntu-2404', buildId: 'b1',
          lines: ['Image: ubuntu-2404.', 'It replaces build b0.'] } },
      ],
    });
    fixture = TestBed.createComponent(PromoteBuildDialogComponent);
    fixture.detectChanges();
    await fixture.whenStable();
  });

  it('says what promoting changes', () => {
    const text = (fixture.nativeElement as HTMLElement).textContent!;
    expect(text).toContain('Promote build b1?');
    expect(text).toContain('It replaces build b0.');
  });

  it('will not promote without a reason', async () => {
    expect((q('gi-promote-confirm') as HTMLButtonElement).disabled).toBeTrue();
    await type('   ');
    expect((q('gi-promote-confirm') as HTMLButtonElement).disabled).toBeTrue();
    fixture.componentInstance.confirm();
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('closes with the trimmed reason', async () => {
    await type('  passed acceptance on esx02 ');
    q('gi-promote-confirm').click();
    expect(ref.close).toHaveBeenCalledOnceWith('passed acceptance on esx02');
  });

  it('caps the reason at 500 characters', async () => {
    expect(q('gi-reason').getAttribute('maxlength')).toBe('500');
    fixture.componentInstance.reason = 'x'.repeat(501);
    expect(fixture.componentInstance.valid()).toBeFalse();
  });

  it('closes with nothing when cancelled', () => {
    q('gi-promote-cancel').click();
    expect(ref.close).toHaveBeenCalledOnceWith();
  });
});
