/**
 * Read-only 2D picture of a range topology: turns Range Designer graph JSON (saved, or
 * generated from the template by GET /ranges/{id}/diagram) into SVG-ready shapes.
 * Pure, so it is unit-tested without a DOM. The 3D viewer reads the same cells.
 */

export const TOPOLOGY_COLORS: Record<string, string> = {
  workstation: '#42A5F5', server: '#66BB6A', dc: '#AB47BC', kali: '#EF5350',
  switch: '#FFA726', router: '#26C6DA', cloud: '#78909C',
  firewall: '#FF7043', seconion: '#5C6BC0', subnet: '#29B6F6', dmz: '#FFCA28',
};

/** Words for each designer type; shown with the OS so the type is not colour-only. */
export const TOPOLOGY_TYPE_NAMES: Record<string, string> = {
  workstation: 'Workstation', server: 'Server', dc: 'Domain controller', kali: 'Attack box',
  switch: 'Switch', router: 'Router', cloud: 'Internet', firewall: 'Firewall', seconion: 'Sensor',
};

export interface Topo2dZone {
  id: string; label: string; cidr: string; x: number; y: number; w: number; h: number; color: string;
}

export interface Topo2dNode {
  id: string;
  label: string;
  /** `label` cut to fit the box; the full name is in `title`. */
  shortLabel: string;
  type: string;
  os: string;
  ip: string;
  /** planned | not built | the range's state (running, stopped, ...) | '' for a saved diagram. */
  status: string;
  x: number; y: number; w: number; h: number;
  color: string;
  /** Everything known about the host, for the tooltip and screen readers. */
  title: string;
}

export interface Topo2dLink {
  id: string; x1: number; y1: number; x2: number; y2: number; ip: string; monitor: boolean;
}

export interface Topo2d {
  zones: Topo2dZone[];
  nodes: Topo2dNode[];
  links: Topo2dLink[];
  /** Content bounds with padding: the SVG viewBox. */
  box: { x: number; y: number; w: number; h: number };
}

const DEFAULT_NODE = { width: 120, height: 80 };
const DEFAULT_ZONE = { width: 600, height: 150 };
const PAD = 24;

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

function isLink(cell: any): boolean {
  return typeof cell?.type === 'string' && cell.type.toLowerCase().includes('link');
}

function fit(text: string, width: number): string {
  const max = Math.max(4, Math.floor((width - 26) / 7));
  return text.length <= max ? text : text.slice(0, max - 1) + '…';
}

/** Address to show: what the build recorded, else what the template plans. */
export function nodeIp(data: Record<string, any>): string {
  const runtime = data?.['runtime'];
  return String((runtime && runtime['ip']) || data?.['ip'] || '');
}

/** Status words; a range's per-VM power is not recorded, so this is the range's state. */
export function nodeStatus(data: Record<string, any>): string {
  const runtime = data?.['runtime'];
  return runtime && typeof runtime['status'] === 'string' ? runtime['status'] : '';
}

/** Where a line from point (px, py) meets the edge of box b, aimed at the box. */
function edgePoint(px: number, py: number, b: { x: number; y: number; w: number; h: number }): [number, number] {
  const cx = Math.min(Math.max(px, b.x + 24), b.x + b.w - 24);
  if (py < b.y) return [cx, b.y];
  if (py > b.y + b.h) return [cx, b.y + b.h];
  return [px < b.x ? b.x : b.x + b.w, Math.min(Math.max(py, b.y + 24), b.y + b.h - 24)];
}

export function layoutTopology2d(diagram: { cells?: unknown } | null | undefined): Topo2d {
  const cells = Array.isArray(diagram?.cells) ? (diagram!.cells as any[]) : [];
  const zones: Topo2dZone[] = [];
  const nodes: Topo2dNode[] = [];
  const boxes = new Map<string, { x: number; y: number; w: number; h: number }>();

  for (const c of cells) {
    if (!c || typeof c !== 'object' || isLink(c)) continue;
    const type = typeof c.nodeType === 'string' ? c.nodeType : 'workstation';
    const data: Record<string, any> = c.nodeData && typeof c.nodeData === 'object' ? c.nodeData : {};
    const x = num(c.position?.x, 0);
    const y = num(c.position?.y, 0);
    const zone = type === 'subnet' || type === 'dmz';
    const size = zone ? DEFAULT_ZONE : DEFAULT_NODE;
    const w = num(c.size?.width, size.width);
    const h = num(c.size?.height, size.height);
    const id = String(c.id ?? `${type}-${zones.length + nodes.length}`);
    const color = TOPOLOGY_COLORS[type] ?? '#66BB6A';
    boxes.set(id, { x, y, w, h });
    if (zone) {
      zones.push({ id, label: String(data['label'] ?? ''), cidr: String(data['cidr'] ?? ''), x, y, w, h, color });
      continue;
    }
    const label = String(data['label'] || c.attrs?.label?.text || TOPOLOGY_TYPE_NAMES[type] || type);
    const os = String(data['os_template'] || data['os'] || '');
    const ip = nodeIp(data);
    const status = nodeStatus(data);
    const title = [label, TOPOLOGY_TYPE_NAMES[type] ?? type, os, ip, status && `status: ${status}`]
      .filter(Boolean).join(' · ');
    nodes.push({ id, label, shortLabel: fit(label, w), type, os, ip, status, x, y, w, h, color, title });
  }

  const links: Topo2dLink[] = [];
  for (const c of cells) {
    if (!isLink(c)) continue;
    const a = boxes.get(String(c.source?.id ?? ''));
    const b = boxes.get(String(c.target?.id ?? ''));
    if (!a || !b) continue;
    const ax = a.x + a.w / 2;
    const ay = a.y + a.h / 2;
    const [x2, y2] = edgePoint(ax, ay, b);
    const [x1, y1] = edgePoint(x2, y2, a);
    const data = c.linkData && typeof c.linkData === 'object' ? c.linkData : {};
    links.push({ id: String(c.id), x1, y1, x2, y2, ip: String(data.ip ?? ''), monitor: !!data.monitor });
  }

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const b of boxes.values()) {
    minX = Math.min(minX, b.x); minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x + b.w); maxY = Math.max(maxY, b.y + b.h);
  }
  const box = boxes.size
    ? { x: minX - PAD, y: minY - PAD, w: maxX - minX + PAD * 2, h: maxY - minY + PAD * 2 }
    : { x: 0, y: 0, w: 800, h: 400 };
  return { zones, nodes, links, box };
}
