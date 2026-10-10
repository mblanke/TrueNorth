/**
 * Range Designer: the Internet/Cloud stencil is the range's Greyspace block (ADR 0007,
 * plan slice 7). Dropping it puts `nodeData.greyspace` on the cell; export
 * (range_topology.diagram_to_template) turns the first such cell into the template's
 * `greyspace:` key, and import (range-designer-yaml-import.ts, range_topology.
 * template_to_diagram) turns that key back into the cell. Pure: no Angular, no JointJS.
 */

/** The template `greyspace:` keys (scenario-engine/schemas/template.schema.json). */
export const GREYSPACE_KEYS = [
  'version', 'corpus_tier', 'site_packs', 'public_prefix', 'npc_profile', 'threat_infra', 'trust_ca', 'network',
] as const;

export interface GreyspaceSettings {
  version: 1;
  corpus_tier: 't0' | 't1' | 't2' | 'full';
  site_packs: string[] | null;
  public_prefix?: string | null;
  npc_profile: 'off' | 'office-day' | 'quiet-night';
  threat_infra: boolean;
  trust_ca: boolean;
  network: string;
  /** Designer only: false keeps the drawing but emits no block. */
  enabled?: boolean;
}

/** Same as range_topology.GREYSPACE_DEFAULTS. */
export const GREYSPACE_DEFAULTS: GreyspaceSettings = {
  version: 1, corpus_tier: 't0', site_packs: null, npc_profile: 'off', threat_infra: true, trust_ca: true,
  network: 'greyspace',
};

export const GREYSPACE_CELL_ID = 'greyspace';
export const GREYSPACE_LABEL = 'Greyspace internet';

/** A template's `greyspace:` mapping as the cell carries it: known keys only, defaults filled. */
export function greyspaceSettings(raw: unknown): GreyspaceSettings {
  const src = (typeof raw === 'object' && raw !== null && !Array.isArray(raw)) ? raw as Record<string, any> : {};
  const known = Object.fromEntries(Object.entries(src).filter(([k]) => (GREYSPACE_KEYS as readonly string[]).includes(k)));
  const out = { ...GREYSPACE_DEFAULTS, ...known } as GreyspaceSettings;
  if (src['enabled'] === false) out.enabled = false;
  return out;
}

/** The block export emits for a cell's settings, or null when the cell has Greyspace off. */
export function greyspaceBlock(settings: GreyspaceSettings | null | undefined): Record<string, unknown> | null {
  if (!settings || settings.enabled === false) return null;
  return Object.fromEntries(GREYSPACE_KEYS.filter(k => k in settings).map(k => [k, (settings as any)[k]]));
}
