import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { fakeAuth } from '@core/auth/fake-auth';
import { AuthService } from '@core/services/auth.service';
import { ContentPortApiService, ProvenStatus } from '@core/services/content-port-api.service';
import { ProvenChipComponent } from './proven-chip.component';

const PROVEN = {
  item_type: 'range_template', item_id: 't1', title: 'SOC lab', proven: true, label: 'Proven', reasons: [],
  conditions: [
    { name: 'certified', title: 'Range certified', met: true, not_applicable: false, reasons: [],
      evidence: [{ kind: 'certification', id: 'rec-1', label: 'Certified for Students: range r1',
        at: '2026-10-20T10:00:00Z' }] },
    { name: 'used', title: 'Used with passing evidence', met: true, not_applicable: false, reasons: [],
      evidence: [{ kind: 'class', id: 'cls-1', label: 'Class C201-26-03 closed with 9 passing Student(s)',
        at: '2026-10-25T10:00:00Z' }] },
    { name: 'fresh', title: 'Not stale', met: true, not_applicable: false, reasons: [], evidence: [] },
  ],
  dependencies: [], golden_builds: [{ catalogue_id: 'srv2022', build_id: 'b-1', checksum: 'a' }],
} as unknown as ProvenStatus;

const NOT_PROVEN = {
  ...PROVEN, proven: false, label: 'Not proven: Stale: Its review is overdue',
  reasons: ['Stale: Its review is overdue'],
  conditions: [{ name: 'fresh', title: 'Not stale', met: false, not_applicable: false,
    reasons: ['Stale: Its review is overdue'], evidence: [] }],
  imported: { origin_key_id: 'ab12cd34ef56ab12', exported_at: '2026-10-20T10:00:00Z',
    imported_at: '2026-10-21T10:00:00Z', proven_at_origin: true, golden_builds: [] },
} as unknown as ProvenStatus;

describe('ProvenChipComponent', () => {
  let fixture: ComponentFixture<ProvenChipComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ContentPortApiService>;
  let auth: ReturnType<typeof fakeAuth>;

  function make(type: string, id: string): void {
    fixture = TestBed.createComponent(ProvenChipComponent);
    fixture.componentRef.setInput('itemType', type);
    fixture.componentRef.setInput('itemId', id);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  beforeEach(async () => {
    api = jasmine.createSpyObj('ContentPortApiService', ['proven']);
    api.proven.and.returnValue(of(PROVEN));
    auth = fakeAuth('instructor');
    await TestBed.configureTestingModule({
      imports: [ProvenChipComponent, NoopAnimationsModule],
      providers: [
        { provide: ContentPortApiService, useValue: api },
        { provide: AuthService, useValue: auth },
      ],
    }).compileComponents();
  });

  it('says Proven and shows the evidence behind Why', () => {
    make('range_template', 't1');
    expect(api.proven).toHaveBeenCalledWith('range_template', 't1');
    expect(el.querySelector('.chip')?.textContent).toContain('Proven');
    expect(el.querySelector('.chip.ok')).not.toBeNull();
    expect(el.querySelector('.conditions')).toBeNull();
    (el.querySelector('button.why') as HTMLButtonElement).click();
    fixture.detectChanges();
    const text = el.querySelector('.conditions')?.textContent ?? '';
    expect(text).toContain('Range certified');
    expect(text).toContain('rec-1');
    expect(text).toContain('Class C201-26-03 closed');
    expect(el.textContent).toContain('srv2022 b-1');
  });

  it('says Not proven with its reasons, and that a ported item must be proven here', () => {
    api.proven.and.returnValue(of(NOT_PROVEN));
    make('course', 'c1');
    expect(el.querySelector('.chip')?.textContent).toContain('Not proven: Stale: Its review is overdue');
    expect(el.querySelector('.chip.ok')).toBeNull();
    expect(el.querySelector('.imported')?.textContent).toContain('proven at origin');
    expect(el.querySelector('.imported')?.textContent).toContain('must be proven on this site');
  });

  it('shows nothing for content that is not ported, or to a Student', () => {
    make('golden_template', 'g1');
    expect(api.proven).not.toHaveBeenCalled();
    expect(el.querySelector('.proven')).toBeNull();
    auth.role.set('student');
    make('course', 'c1');
    expect(api.proven).not.toHaveBeenCalled();
    expect(el.querySelector('.proven')).toBeNull();
  });

  it('shows a refusal in words', () => {
    api.proven.and.returnValue(throwError(() => new HttpErrorResponse({ status: 404,
      error: { detail: 'No course c1 here' } })));
    make('course', 'c1');
    expect(el.querySelector('.err')?.textContent).toContain('No course c1 here');
  });
});
