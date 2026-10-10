/**
 * Range Designer: import a range-template YAML file back onto the canvas.
 *
 * Export is server-side (POST /templates/from-diagram -> range_topology.diagram_to_template),
 * so this is the inverse of that function: `templateToDiagram` mirrors
 * range_topology.template_to_diagram (full-node templates) and the assets fallback of
 * build_template_diagram. An exported file therefore re-imports to the same zones,
 * nodes, OS, specs, services, IPs and VLANs, and exporting the result again produces
 * the same template.
 *
 * The YAML text is parsed and schema-checked by POST /templates/validate (the same
 * validator the template editor uses), which returns the parsed mapping as
 * `normalized`. The web app has no YAML parser of its own and this avoids adding one;
 * a syntax error comes back from the server as "Invalid YAML: ... line N, column M".
 *
 * Everything here is pure (no Angular, no JointJS) so it can be unit-tested.
 */
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { GREYSPACE_CELL_ID, GREYSPACE_LABEL, greyspaceSettings } from './greyspace-stencil';

/** Same cap as the server's safe_yaml.MAX_YAML_BYTES. */
export const MAX_IMPORT_BYTES = 1024 * 1024;
/** Same cap as range_topology.MAX_DIAGRAM_CELLS; a `count` must not explode the canvas. */
export const MAX_IMPORT_CELLS = 2000;

/** The subset of POST /templates/validate's response this needs. */
export interface TemplateValidation {
  valid: boolean;
  errors: { path: string; message: string }[];
  normalized: Record<string, any> | null;
  warnings?: string[];
}

export interface DiagramJson {
  cells: Record<string, any>[];
}

export interface TemplateImport {
  diagram: DiagramJson;
  nodeCount: number;
  zoneCount: number;
  /** Things in the file the designer cannot show; reported, never silently dropped. */
  notes: string[];
}

export type YamlImportOutcome =
  | ({ ok: true; warnings: string[] } & TemplateImport)
  | { ok: false; title: string; messages: string[] };

export class TemplateImportError extends Error {}

/* ---- cell builders: mirror range_topology._cell_zone / _cell_node ---------------- */

const NODE_COLORS: Record<string, string> = {
  workstation: '#42A5F5', server: '#66BB6A', dc: '#AB47BC', kali: '#EF5350',
  switch: '#FFA726', router: '#26C6DA', cloud: '#78909C',
  firewall: '#FF7043', seconion: '#5C6BC0', subnet: '#29B6F6', dmz: '#FFCA28',
};
const NODE_W = 120;
const NODE_H = 80;
const GRID_COLS = 4;
const CELL_W = 150;
const CELL_H = 95;

/** range_topology._ROLE_STENCIL: template role -> [designer type, default OS]. */
const ROLE_STENCIL: Record<string, [string, string]> = {
  dc: ['dc', 'windows-server-2022'],
  domain_controller: ['dc', 'windows-server-2022'],
  server: ['server', 'ubuntu-24.04'],
  workstation: ['workstation', 'windows-11'],
  client: ['workstation', 'windows-11'],
  kali: ['kali', 'kali-2024'],
  attacker: ['kali', 'kali-2024'],
  firewall: ['firewall', 'pfsense'],
  gateway: ['firewall', 'pfsense'],
  router: ['router', 'vyos'],
  sensor: ['seconion', 'security-onion'],
  ids: ['seconion', 'security-onion'],
};

/** Node keys the designer has a field for; anything else rides in template_extra. */
const NODE_FIELDS = new Set(['id', 'name', 'role', 'os', 'vlan', 'ip', 'specs', 'services', 'count']);
const VLAN_FIELDS = new Set(['id', 'name', 'cidr']);
/** Top-level keys the designer either uses or regenerates on export. */
const KNOWN_TOP_LEVEL = new Set(['name', 'version', 'network', 'nodes', 'assets', 'source', 'greyspace']);

/** Mirror of range_topology.greyspace_cell: the Internet/Cloud cell carrying the block. */
function greyspaceCell(block: unknown, x: number, y: number, taken: Set<string>): Record<string, any> {
  const cell = nodeCell(uniqueId(GREYSPACE_CELL_ID, taken), GREYSPACE_LABEL, 'cloud', '', '', x, y);
  cell['nodeData']['greyspace'] = greyspaceSettings(block);
  return cell;
}

function zoneCell(
  id: string, label: string, cidr: string, x: number, y: number,
  dmz: boolean, width: number, height: number, vlan: number | null,
): Record<string, any> {
  const color = NODE_COLORS[dmz ? 'dmz' : 'subnet'];
  const data: Record<string, any> = { label, cidr };
  if (vlan !== null) data['vlan'] = vlan;
  return {
    type: 'standard.Rectangle',
    id,
    position: { x, y },
    size: { width, height },
    angle: 0,
    nodeType: dmz ? 'dmz' : 'subnet',
    nodeData: data,
    attrs: {
      body: { fill: color + '15', stroke: color, strokeWidth: 2, strokeDasharray: '8 4', rx: 12, ry: 12 },
      label: {
        text: cidr ? `${label}  (${cidr})` : label, fill: color, fontSize: 14,
        fontFamily: 'Inter, Segoe UI, sans-serif', fontWeight: 'bold',
        textAnchor: 'start', textVerticalAnchor: 'top', refX: 12, refY: 8,
      },
    },
  };
}

function nodeCell(
  id: string, label: string, nodeType: string, os: string, ip: string, x: number, y: number,
): Record<string, any> {
  const color = NODE_COLORS[nodeType] ?? '#66BB6A';
  const port = { fill: color, stroke: 'var(--border)', strokeWidth: 1, r: 5, magnet: true };
  return {
    type: 'standard.Rectangle',
    id,
    position: { x, y },
    size: { width: NODE_W, height: NODE_H },
    angle: 0,
    nodeType,
    nodeData: { label, ip, os_template: os },
    attrs: {
      body: { fill: 'var(--bg-card)', stroke: color, strokeWidth: 2, rx: 8, ry: 8, filter: 'none' },
      label: {
        text: ip ? `${label}\n${ip}` : label, fill: 'var(--text-primary)', fontSize: 12,
        fontFamily: 'Inter, Segoe UI, sans-serif', textAnchor: 'middle', textVerticalAnchor: 'top',
        refX: '50%', refY: '62%',
      },
    },
    ports: {
      groups: {
        in: { position: 'left', attrs: { circle: { ...port } }, label: { position: { name: 'outside' } } },
        out: { position: 'right', attrs: { circle: { ...port } }, label: { position: { name: 'outside' } } },
      },
      items: [{ group: 'in', id: 'in1' }, { group: 'out', id: 'out1' }],
    },
  };
}

/* ---- helpers ---------------------------------------------------------------------- */

function isMapping(v: unknown): v is Record<string, any> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Python str() of a scalar as PyYAML parsed it; null/undefined -> ''. */
function str(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  return String(v);
}

/** range_topology._int_or_none. */
function intOrNull(v: unknown): number | null {
  if (typeof v === 'boolean' || v === null || v === undefined) return null;
  const s = String(v).trim();
  return /^[+-]?\d+$/.test(s) ? parseInt(s, 10) : null;
}

/** int(x.get('count', 1) or 1), never below 1; a non-number is reported, not fatal. */
function countOf(node: Record<string, any>, notes: string[], label: string): number {
  const raw = node['count'];
  if (raw === undefined || raw === null || raw === '' || raw === 0) return 1;
  const n = intOrNull(raw);
  if (n === null) {
    notes.push(`${label}: count ${JSON.stringify(raw)} is not a number; imported once`);
    return 1;
  }
  return Math.max(1, n);
}

/** range_topology._node_type_for: designer stencil type from role, else OS. */
export function nodeTypeFor(role: string, os: string): string {
  const r = role.toLowerCase();
  const o = os.toLowerCase();
  if (r in ROLE_STENCIL) return ROLE_STENCIL[r][0];
  if (o.startsWith('kali')) return 'kali';
  if (o.startsWith('pfsense')) return 'firewall';
  if (o.startsWith('vyos')) return 'router';
  if (o.replace(/-/g, '').startsWith('securityonion')) return 'seconion';
  if (o.startsWith('windows') && !o.includes('server')) return 'workstation';
  return 'server';
}

function uniqueId(wanted: string, taken: Set<string>): string {
  let id = wanted;
  for (let n = 2; taken.has(id); n++) id = `${wanted}~${n}`;
  taken.add(id);
  return id;
}

/* ---- the conversion --------------------------------------------------------------- */

/**
 * Turn a parsed template mapping into designer diagram JSON.
 *
 * Unknown node / VLAN keys are kept in `nodeData.template_extra` (export puts them
 * back). Unknown top-level keys, links, and malformed entries are listed in `notes`.
 * Throws TemplateImportError when there is nothing usable to import.
 */
export function templateToDiagram(doc: unknown): TemplateImport {
  if (!isMapping(doc)) throw new TemplateImportError('The file is not a YAML mapping (expected keys like name:, nodes:).');
  const notes: string[] = [];

  const ignored = Object.keys(doc).filter(k => !KNOWN_TOP_LEVEL.has(k));
  if (ignored.length) {
    notes.push(`Not shown in the designer and not kept on export: ${ignored.join(', ')}`);
  }

  const rawNodes = Array.isArray(doc['nodes']) ? doc['nodes'] : [];
  if (doc['nodes'] !== undefined && doc['nodes'] !== null && !Array.isArray(doc['nodes'])) {
    notes.push('nodes: is not a list; ignored');
  }
  const nodeDicts = rawNodes.filter(isMapping);
  const skipped = rawNodes.length - nodeDicts.length;
  const assets = Array.isArray(doc['assets']) ? doc['assets'] : [];

  if (nodeDicts.length && !assets.length) {
    if (skipped) notes.push(`${skipped} node entr${skipped === 1 ? 'y was' : 'ies were'} not a mapping; skipped`);
    return fullNodesToDiagram(doc, nodeDicts, notes);
  }
  if (assets.length || rawNodes.length) {
    return assetsToDiagram(doc, assets, rawNodes, notes);
  }
  throw new TemplateImportError('The template declares no nodes or assets, so there is nothing to draw.');
}

/** Mirror of range_topology.template_to_diagram. */
function fullNodesToDiagram(doc: Record<string, any>, nodes: Record<string, any>[], notes: string[]): TemplateImport {
  const network = isMapping(doc['network']) ? doc['network'] : {};
  const vlans = (Array.isArray(network['vlans']) ? network['vlans'] : []).filter(isMapping);
  const vlanNames = vlans.map(v => str(v['name']));

  const instances: { id: string; node: Record<string, any>; zone: string; count: number }[] = [];
  for (const node of nodes) {
    const base = str(node['id'] || node['name']) || 'node';
    const count = countOf(node, notes, base);
    const zone = node['vlan'] === undefined || node['vlan'] === null ? 'default' : str(node['vlan']);
    if (instances.length + count > MAX_IMPORT_CELLS) {
      throw new TemplateImportError(
        `The template expands to more than ${MAX_IMPORT_CELLS} nodes (counts included); the designer limit is ${MAX_IMPORT_CELLS}.`,
      );
    }
    for (let r = 0; r < count; r++) instances.push({ id: count > 1 ? `${base}-${r}` : base, node, zone, count });
  }
  const extraZones = [...new Set(instances.map(i => i.zone))].filter(z => !vlanNames.includes(z)).sort();
  const zoneOrder = [...vlanNames, ...extraZones];

  const taken = new Set<string>();
  const zoneW = 40 + GRID_COLS * CELL_W;
  const zoneCells: Record<string, any>[] = [];
  const slot = new Map<string, { top: number; idx: number }>();
  const zoneVid = new Map<string, number | null>();
  let y = 40;
  zoneOrder.forEach((zname, zi) => {
    if (slot.has(zname)) return; // two VLANs with one name: the first wins, like the server
    const meta = vlans.find(v => str(v['name']) === zname) ?? {};
    const members = instances.filter(i => i.zone === zname).length;
    const rows = Math.max(1, Math.ceil(members / GRID_COLS));
    const vid = intOrNull(meta['id']);
    const cell = zoneCell(
      uniqueId(`zone-${zi}`, taken), zname, str(meta['cidr']), 40, y,
      zname.toLowerCase().includes('dmz'), zoneW, 50 + rows * CELL_H, vid,
    );
    const extra = Object.fromEntries(Object.entries(meta).filter(([k]) => !VLAN_FIELDS.has(k)));
    if (Object.keys(extra).length) cell['nodeData']['template_extra'] = extra;
    zoneCells.push(cell);
    slot.set(zname, { top: y, idx: 0 });
    zoneVid.set(zname, vid);
    y += 50 + rows * CELL_H + 40;
  });

  const nodeCells: Record<string, any>[] = [];
  for (const { id, node, zone, count } of instances) {
    const s = slot.get(zone)!;
    const idx = s.idx++;
    const os = str(node['os']);
    const role = str(node['role']);
    const ip = count === 1 ? str(node['ip']) : '';
    if (!os) notes.push(`${id}: no os set; export will skip it until an OS template is chosen`);
    const cell = nodeCell(
      uniqueId(id, taken), str(node['name']) || id, nodeTypeFor(role, os), os, ip,
      60 + (idx % GRID_COLS) * CELL_W, s.top + 40 + Math.floor(idx / GRID_COLS) * CELL_H,
    );
    const data = cell['nodeData'];
    data['hostname'] = id;
    if (role) data['role'] = role;
    const specs = isMapping(node['specs']) ? node['specs'] : {};
    for (const [src, dst] of [['cores', 'vcpu'], ['memory_mb', 'ram_mb'], ['disk_gb', 'disk_gb']]) {
      if (specs[src] !== undefined && specs[src] !== null) data[dst] = str(specs[src]);
    }
    const extraSpecs = Object.fromEntries(
      Object.entries(specs).filter(([k]) => !['cores', 'memory_mb', 'disk_gb'].includes(k)),
    );
    const vid = zoneVid.get(zone);
    if (vid !== null && vid !== undefined) data['vlan'] = String(vid);
    const services = node['services'];
    if (Array.isArray(services) && services.length) data['services'] = services.map(str).join(',');
    const extra: Record<string, any> = Object.fromEntries(Object.entries(node).filter(([k]) => !NODE_FIELDS.has(k)));
    if (Object.keys(extraSpecs).length) extra['specs'] = extraSpecs;
    if (Object.keys(extra).length) data['template_extra'] = extra;
    nodeCells.push(cell);
  }
  const nodeCount = nodeCells.length;
  if (isMapping(doc['greyspace'])) nodeCells.push(greyspaceCell(doc['greyspace'], 40 + zoneW + 40, 40, taken));
  return { diagram: { cells: [...zoneCells, ...nodeCells] }, nodeCount, zoneCount: zoneCells.length, notes };
}

/** Mirror of build_template_diagram's assets path: a gateway plus one zone of hosts. */
function assetsToDiagram(
  doc: Record<string, any>, assets: unknown[], nodes: unknown[], notes: string[],
): TemplateImport {
  const name = str(doc['name']) || 'template';
  const network = isMapping(doc['network']) ? doc['network'] : {};
  const cidr = str(network['cidr']) || '10.0.0.0/24';
  const hosts: [string, string][] = [];
  for (const asset of assets) {
    if (!isMapping(asset)) continue;
    const role = str(asset['role'] || asset['type']) || 'server';
    const count = countOf(asset, notes, role);
    if (hosts.length + count > MAX_IMPORT_CELLS) {
      throw new TemplateImportError(`The template expands to more than ${MAX_IMPORT_CELLS} hosts.`);
    }
    for (let i = 0; i < count; i++) hosts.push([count > 1 ? `${role}${i + 1}` : role, role]);
  }
  for (const node of nodes) {
    if (isMapping(node)) hosts.push([str(node['label'] || node['name']) || 'node', str(node['role'] || node['type']) || 'server']);
    else if (typeof node === 'string') hosts.push([node, 'server']);
  }
  const shown = hosts.slice(0, 24);
  if (hosts.length > shown.length) {
    notes.push(`Asset templates are drawn as a starter sketch: ${shown.length} of ${hosts.length} hosts shown`);
  }
  notes.push('Assets-style template: hosts were laid out with default OS and addresses; review before saving');
  const cells = [
    nodeCell('fw01', 'Gateway', 'firewall', 'pfsense', '10.0.0.1', 280, 20),
    zoneCell('zone-0', name, cidr, 40, 130, false, 640, 150, null),
  ];
  shown.forEach(([label, role], i) => {
    const [type, os] = ROLE_STENCIL[role.toLowerCase()] ?? ['server', 'ubuntu-24.04'];
    cells.push(nodeCell(`n-${i}`, label, type, os, `10.0.0.${10 + i}`, 70 + (i % 4) * 150, 175 + Math.floor(i / 4) * 95));
  });
  if (isMapping(doc['greyspace'])) cells.push(greyspaceCell(doc['greyspace'], 720, 20, new Set(cells.map(c => c['id']))));
  return { diagram: { cells }, nodeCount: shown.length + 1, zoneCount: 1, notes };
}

/* ---- orchestration ---------------------------------------------------------------- */

/** Problems visible without parsing: empty, too large, or not text. Null when fine. */
export function precheckYamlText(text: string): string | null {
  if (!text.trim()) return 'The file is empty.';
  if (new Blob([text]).size > MAX_IMPORT_BYTES) return `The file is larger than ${MAX_IMPORT_BYTES / 1024 / 1024} MiB.`;
  if (text.includes('\u0000')) return 'The file is not text (it contains NUL bytes).';
  return null;
}

function errorText(e: { path: string; message: string }): string {
  return e.path ? `${e.path}: ${e.message}` : e.message;
}

function httpErrorText(err: any): string {
  const detail = err?.error?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d: any) => d?.msg ?? JSON.stringify(d)).join('; ');
  if (err?.status === 0) return 'The control plane could not be reached.';
  if (err?.status === 403) return 'You do not have permission to validate templates.';
  return err?.message || 'Unknown error';
}

/**
 * Validate `text` with the server, then convert. Never throws and never emits a
 * diagram for a file that failed validation: the caller shows `messages` instead.
 */
export function importTemplateYaml(
  text: string,
  validate: (yaml: string) => Observable<TemplateValidation>,
): Observable<YamlImportOutcome> {
  const pre = precheckYamlText(text);
  if (pre) return of({ ok: false, title: 'Cannot import this file', messages: [pre] });
  return validate(text).pipe(
    map((res): YamlImportOutcome => {
      const errors = Array.isArray(res?.errors) ? res.errors : [];
      if (!res?.valid || errors.length) {
        const syntax = !res?.normalized;
        return {
          ok: false,
          title: syntax ? 'The file is not valid YAML' : 'The template failed validation',
          messages: errors.length ? errors.map(errorText) : ['The validator rejected the file without a reason.'],
        };
      }
      try {
        const out = templateToDiagram(res.normalized);
        return { ok: true, warnings: Array.isArray(res.warnings) ? res.warnings : [], ...out };
      } catch (e) {
        return { ok: false, title: 'Nothing to import', messages: [(e as Error).message] };
      }
    }),
    catchError(err => of<YamlImportOutcome>({
      ok: false, title: 'Could not validate the file', messages: [httpErrorText(err)],
    })),
  );
}
