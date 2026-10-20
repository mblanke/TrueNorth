/**
 * Saved diagrams are JointJS graph JSON, and JointJS serialises each element's port
 * groups into it. jointjs 3 accepted a bare string for a port label position
 * (`label: { position: 'outside' }`); @joint/core 4 throws on it. Every diagram saved
 * before the upgrade carries that form, so it is rewritten on load. Without this,
 * fromJSON() throws, the designer reports the diagram as corrupt and starts blank,
 * and the next Save overwrites the stored topology.
 */
export function upgradeDiagramJson<T>(diagram: T): T {
  const copy = structuredClone(diagram) as any;
  const cells = copy?.cells;
  if (!Array.isArray(cells)) return copy;
  for (const cell of cells) {
    const ports = cell?.ports;
    if (!ports || typeof ports !== 'object') continue;
    for (const group of Object.values(ports.groups ?? {})) upgradeLabel(group);
    for (const item of Array.isArray(ports.items) ? ports.items : []) upgradeLabel(item);
  }
  return copy;
}

function upgradeLabel(holder: any): void {
  const label = holder?.label;
  if (label && typeof label.position === 'string') {
    label.position = { name: label.position };
  }
}

/**
 * A diagram GET /ranges/{id}/diagram generated from the template carries what the last
 * build recorded in `nodeData.runtime`. That is a reading, not a design: it is dropped
 * before the designer loads the cells, so a Save never freezes it into the range.
 */
export function withoutRuntime<T extends { cells?: unknown[] }>(diagram: T | null | undefined): T | null | undefined {
  if (!diagram || !Array.isArray(diagram.cells)) return diagram;
  const cells = diagram.cells.map(cell => {
    const data = (cell as { nodeData?: Record<string, unknown> })?.nodeData;
    if (!data || typeof data !== 'object' || !('runtime' in data)) return cell;
    const rest = { ...data };
    delete rest['runtime'];
    return { ...(cell as object), nodeData: rest };
  });
  return { ...diagram, cells };
}
