/**
 * Undo/redo stack for the Range Designer.
 *
 * Deliberately DOM-free and JointJS-free: it stores opaque snapshots (whatever
 * `graph.toJSON()` returned) and only tracks where the cursor sits in the stack.
 * That keeps it unit-testable without a paper, a canvas or a browser.
 *
 * Semantics:
 *   - `push` truncates any redo tail — branching off an undone state discards
 *     the abandoned future, the way every editor behaves;
 *   - the stack is capped, dropping the OLDEST entries first, so a long editing
 *     session cannot grow memory without bound;
 *   - `undo`/`redo` move the cursor and return the snapshot to restore, or
 *     `null` when there is nothing to move to. The caller decides how to apply
 *     it, and is responsible for not re-pushing the restored state.
 */
export class GraphHistory {
  private stack: unknown[] = [];
  /** Index of the snapshot currently applied; -1 when the stack is empty. */
  private index = -1;

  constructor(private cap = 50) {
    this.cap = Math.max(1, Math.floor(cap) || 1);
  }

  /** Record a new state, discarding anything that was redoable. */
  push(snapshot: unknown): void {
    if (this.index < this.stack.length - 1) {
      this.stack = this.stack.slice(0, this.index + 1);
    }
    this.stack.push(snapshot);
    const overflow = this.stack.length - this.cap;
    if (overflow > 0) {
      this.stack.splice(0, overflow);
    }
    this.index = this.stack.length - 1;
  }

  /** Step back one state. Returns the snapshot to restore, or null. */
  undo(): unknown | null {
    if (!this.canUndo) return null;
    this.index -= 1;
    return this.stack[this.index] ?? null;
  }

  /** Step forward one state. Returns the snapshot to restore, or null. */
  redo(): unknown | null {
    if (!this.canRedo) return null;
    this.index += 1;
    return this.stack[this.index] ?? null;
  }

  /** True when there is an earlier state to return to. */
  get canUndo(): boolean {
    return this.index > 0;
  }

  /** True when a state was undone and has not been superseded by a push. */
  get canRedo(): boolean {
    return this.index >= 0 && this.index < this.stack.length - 1;
  }

  /**
   * Drop all history. With a snapshot, that snapshot becomes the new baseline
   * (used after loading a saved diagram: the loaded state is not undoable, but
   * everything done on top of it is).
   */
  reset(snapshot?: unknown): void {
    this.stack = snapshot === undefined ? [] : [snapshot];
    this.index = this.stack.length - 1;
  }

  /** Number of retained snapshots. Exposed for tests and diagnostics. */
  get size(): number {
    return this.stack.length;
  }
}
