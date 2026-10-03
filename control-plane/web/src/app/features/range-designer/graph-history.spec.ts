import { GraphHistory } from './graph-history';

describe('GraphHistory', () => {
  it('walks back and forward through pushed snapshots', () => {
    const h = new GraphHistory();
    h.push('a');
    h.push('b');
    h.push('c');

    expect(h.undo()).toBe('b');
    expect(h.undo()).toBe('a');
    expect(h.redo()).toBe('b');
    expect(h.redo()).toBe('c');
  });

  it('returns null instead of walking off either end', () => {
    const h = new GraphHistory();
    expect(h.undo()).toBeNull();
    expect(h.redo()).toBeNull();

    h.push('only');
    // A single snapshot is the baseline, not something to undo past.
    expect(h.undo()).toBeNull();
    expect(h.redo()).toBeNull();
  });

  it('truncates the redo tail when a new push follows an undo', () => {
    const h = new GraphHistory();
    h.push('a');
    h.push('b');
    h.push('c');

    expect(h.undo()).toBe('b');
    expect(h.canRedo).toBe(true);

    h.push('b2');
    expect(h.canRedo).toBe(false);
    expect(h.redo()).toBeNull();
    expect(h.undo()).toBe('b');
    expect(h.redo()).toBe('b2');
  });

  it('caps the stack by dropping the oldest entries', () => {
    const h = new GraphHistory(3);
    h.push('a');
    h.push('b');
    h.push('c');
    h.push('d');

    expect(h.size).toBe(3);
    expect(h.undo()).toBe('c');
    expect(h.undo()).toBe('b');
    // 'a' was evicted, so 'b' is now the oldest reachable state.
    expect(h.undo()).toBeNull();
    expect(h.canUndo).toBe(false);
  });

  it('never lets a degenerate cap produce an empty stack', () => {
    const h = new GraphHistory(0);
    h.push('a');
    h.push('b');
    expect(h.size).toBe(1);
    expect(h.canUndo).toBe(false);
  });

  it('reports canUndo/canRedo in step with the cursor', () => {
    const h = new GraphHistory();
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);

    h.push({ cells: [] });
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);

    h.push({ cells: [1] });
    expect(h.canUndo).toBe(true);
    expect(h.canRedo).toBe(false);

    h.undo();
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(true);

    h.redo();
    expect(h.canUndo).toBe(true);
    expect(h.canRedo).toBe(false);
  });

  it('resets to an empty stack or to a fresh baseline', () => {
    const h = new GraphHistory();
    h.push('a');
    h.push('b');

    h.reset();
    expect(h.size).toBe(0);
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);
    expect(h.undo()).toBeNull();

    h.reset('loaded');
    expect(h.size).toBe(1);
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);

    h.push('edited');
    expect(h.canUndo).toBe(true);
    expect(h.undo()).toBe('loaded');
  });

  it('preserves snapshot identity rather than cloning', () => {
    const first = { cells: [{ id: 1 }] };
    const second = { cells: [{ id: 2 }] };
    const h = new GraphHistory();
    h.push(first);
    h.push(second);
    expect(h.undo()).toBe(first);
  });
});
