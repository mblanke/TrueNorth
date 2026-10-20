import { evidenceText, humanizeRef, objectiveKind, objectiveName } from './objective-display';

describe('objective display (issue #201)', () => {
  it('names an objective by its name, not its id', () => {
    expect(objectiveName({ ref_id: 'detect_c2', description: 'Detect the C2 beacon' })).toBe('Detect the C2 beacon');
  });

  it('turns a bare id into words when the objective has no name', () => {
    expect(objectiveName({ ref_id: 'contain_ws07', description: '' })).toBe('Contain WS07');
    // scenario_objectives.parse stores the id as the name when the YAML gives none
    expect(objectiveName({ ref_id: 'contain_ws07', description: 'contain_ws07' })).toBe('Contain WS07');
    expect(humanizeRef('deliverable-report')).toBe('Deliverable report');
    expect(humanizeRef('crit-2')).toBe('Critical event 2');
    expect(humanizeRef('detect_c2')).toBe('Detect C2');
  });

  it('labels instructor-judged objectives as sign-off, not by their type', () => {
    expect(objectiveKind({ validator: 'manual_ack', type: 'response' })).toBe('Instructor sign-off');
    expect(objectiveKind({ validator: 'deliverable_check', objective_type: 'deliverable' }))
      .toBe('Deliverable · instructor sign-off');
    expect(objectiveKind({ validator: 'validate.opensearch_query', type: 'detection' })).toBe('Detection');
    expect(objectiveKind({ validator: null, type: 'response' })).toBe('Instructor sign-off');
    expect(objectiveKind({ validator: 'greyspace_breadcrumb', type: 'detection' })).toBe('Greyspace breadcrumb');
  });

  it('reads a Student detection as who detected what', () => {
    const raw = JSON.stringify({ source: 'student_detection', submission_id: 'x1', student_id: 'u1',
      student: 'Sam Student', events_matched: 3, on_target: 3 });
    expect(evidenceText(raw)).toBe('Detected by Sam Student: 3 events matched, 3 of the attack');
  });

  it('marks a staff detection as staff', () => {
    const raw = JSON.stringify({ source: 'staff_detection', user: 'Ines Instructor', events_matched: 1, on_target: 0 });
    expect(evidenceText(raw)).toBe('Detected by Ines Instructor (staff check): 1 event matched, 0 of the attack');
  });

  it('keeps plain text and turns other JSON into labelled fields, never raw braces', () => {
    expect(evidenceText('Acknowledged by Sgt Rivera')).toBe('Acknowledged by Sgt Rivera');
    expect(evidenceText('')).toBe('');
    expect(evidenceText(null)).toBe('');
    const other = evidenceText(JSON.stringify({ source: 'event_store', matched_count: 4, nested: { a: 1 } }));
    expect(other).toBe('Source: event store · Matched count: 4');
    expect(other).not.toContain('{');
  });
});
