import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';

import { ScoringComponent } from './scoring.component';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { Exercise, Objective } from '@core/models';

const exercise: Exercise = {
  id: 'ex1', name: 'Blue Team Day 1', range_id: 'r1', scenario_id: 's1', state: 'running',
  total_score: 25, max_score: 100, created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z',
};

const objectives: Objective[] = [
  {
    id: 'o1', exercise_id: 'ex1', ref_id: 'obj-a', objective_type: 'detection',
    description: 'Detect C2 callback', validator: 'manual', points: 60, achieved: false,
  },
  {
    id: 'o2', exercise_id: 'ex1', ref_id: 'obj-b', objective_type: 'detection',
    description: 'Contain host', validator: 'manual', points: 25, achieved: true,
    achieved_at: '2026-10-06T01:00:00Z', evidence: 'Acknowledged by Sgt Rivera',
  },
];

/** Objective acknowledgement is objective:ack — instructors and admins only (ADR 0005 §4). */
describe('ScoringComponent acknowledge control', () => {
  let fixture: ComponentFixture<ScoringComponent>;

  function render(canAck: boolean): HTMLElement {
    const api = jasmine.createSpyObj('ApiService', [
      'listExercises', 'getExercise', 'listObjectives', 'getAAR', 'ackObjective', 'generateAAR',
    ]);
    api.listExercises.and.returnValue(of([]));
    api.getExercise.and.returnValue(of(exercise));
    api.listObjectives.and.returnValue(of(objectives));
    api.getAAR.and.returnValue(throwError(() => new Error('none')));

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ScoringComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: { canAcknowledgeObjectives: signal(canAck) } },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
      ],
    });
    fixture = TestBed.createComponent(ScoringComponent);
    fixture.componentInstance.selectedExerciseId = 'ex1';
    fixture.componentInstance.loadExercise();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const ackButtons = (el: HTMLElement) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes('Acknowledge'));

  it('hides Acknowledge from a Student, who still sees the objectives', () => {
    const el = render(false);
    expect(el.textContent).toContain('Detect C2 callback');
    expect(ackButtons(el).length).toBe(0);
  });

  it('shows Acknowledge to an instructor on unachieved objectives only', () => {
    const el = render(true);
    expect(ackButtons(el).length).toBe(1);
  });
});
