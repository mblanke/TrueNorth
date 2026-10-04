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
