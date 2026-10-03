import { SoftwareCatalogue, SoftwareEntry } from '@core/services/api.service';

/**
 * Helpers for the designer's `services` chip input. A node's services are stored as one
 * comma-separated string in nodeData.services; the worker resolves each name against
 * content/catalogue/software_catalogue.yaml for the guest's OS family.
 */

/** `windows` | `linux` get installs; `appliance` (router/firewall images) gets none. */
export type OsFamily = 'windows' | 'linux' | 'appliance';

const APPLIANCE_PREFIXES = ['pfsense', 'opnsense', 'vyos'];
const APPLIANCE_TYPES = new Set(['firewall', 'router', 'gateway']);

/**
 * The family the worker will treat a node as (mirrors vsphere_infra.os_family):
 * an OS containing "win" is windows; pfSense/OPNsense/VyOS or a router/firewall node is
 * an appliance; anything else is linux. No OS chosen yet gives null.
 */
export function osFamilyOf(osTemplate: string, nodeType = ''): OsFamily | null {
  const os = (osTemplate || '').trim().toLowerCase();
  if (os.includes('win')) return 'windows';
  if (APPLIANCE_PREFIXES.some(p => os.startsWith(p)) || APPLIANCE_TYPES.has(nodeType.toLowerCase())) {
    return 'appliance';
  }
  return os ? 'linux' : null;
}

/** nodeData.services -> chip values. Same split the designer has always used. */
export function parseServices(raw: string | undefined | null): string[] {
  return (raw || '').split(',').map(s => s.trim()).filter(s => s);
}

/** Chip values -> nodeData.services. Unchanged storage format: comma-joined. */
export function joinServices(values: string[]): string {
  return values.join(',');
}

export interface ServiceSuggestion {
  value: string;
  kind: 'software' | 'role';
  /** Aliases, shown so a search for "chrome" explains why "googlechrome" appeared. */
  aliases: string[];
}

function entryFor(catalogue: SoftwareCatalogue | null, name: string): SoftwareEntry | undefined {
  const key = name.trim().toLowerCase();
  return catalogue?.software.find(e => e.name === key || e.aliases.includes(key));
}

/**
 * Autocomplete options for one node. Software is limited to entries the catalogue can
 * install on the node's family (none for appliances; all while the OS is unset). Role
 * names always qualify. Matches the query against names and aliases, and leaves out
 * anything the node already has (by name or by alias).
 */
export function suggestServices(
  catalogue: SoftwareCatalogue | null,
  family: OsFamily | null,
  query: string,
  selected: string[],
): ServiceSuggestion[] {
  if (!catalogue) return [];
  const q = (query || '').trim().toLowerCase();
  const taken = new Set<string>();
  for (const s of selected) {
    const key = s.trim().toLowerCase();
    taken.add(key);
    const e = entryFor(catalogue, key);
    if (e) taken.add(e.name);
  }
  const matches = (name: string, aliases: string[]) =>
    !q || name.includes(q) || aliases.some(a => a.includes(q));

  const software: ServiceSuggestion[] = catalogue.software
    .filter(e => family === null || (family !== 'appliance' && e.os_families.includes(family)))
    .filter(e => !taken.has(e.name) && matches(e.name, e.aliases))
    .map(e => ({ value: e.name, kind: 'software' as const, aliases: e.aliases }));
  const roles: ServiceSuggestion[] = catalogue.roles
    .filter(r => !taken.has(r) && matches(r, []))
    .map(r => ({ value: r, kind: 'role' as const, aliases: [] }));
  return [...software, ...roles];
}

/**
 * Why a chip will not install, or null if it will (or is a role / free text the
 * catalogue does not know, which the worker reports at deploy time).
 */
export function installWarning(
  catalogue: SoftwareCatalogue | null,
  family: OsFamily | null,
  value: string,
): string | null {
  if (!catalogue || family === null) return null;
  const key = value.trim().toLowerCase();
  if (catalogue.roles.includes(key)) return null;
  const e = entryFor(catalogue, key);
  if (!e) return 'Not in the software catalogue: nothing is installed for it';
  if (family === 'appliance') return 'Appliances do not take software installs';
  if (!e.os_families.includes(family)) return `No ${family} install in the catalogue: deploy skips it`;
  return null;
}

/**
 * Why a chip may fail to install in a range with no internet, or null. Only Windows
 * nodes: the flag is about the Chocolatey package (a wrapper that downloads its vendor
 * installer at install time). Unknown names and roles are installWarning's business.
 */
export function offlineWarning(
  catalogue: SoftwareCatalogue | null,
  family: OsFamily | null,
  value: string,
): string | null {
  if (!catalogue || family !== 'windows') return null;
  const e = entryFor(catalogue, value);
  if (!e || !e.os_families.includes('windows') || e.offline !== false) return null;
  return 'Not offline-ready: its package downloads the vendor installer at install time, '
    + 'so it may fail in a range with no internet (internalize it in content/choco)';
}

/** The chip's warning: one that stops the install first, else the offline one. */
export function chipWarning(
  catalogue: SoftwareCatalogue | null,
  family: OsFamily | null,
  value: string,
): string | null {
  return installWarning(catalogue, family, value) ?? offlineWarning(catalogue, family, value);
}
