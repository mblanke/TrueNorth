/**
 * The OU and security-group types the API accepts (control-plane/api/app/schemas.py
 * OUIn / OUUpdate and SecurityGroupIn / SecurityGroupUpdate; models.OUType and
 * SecurityGroupType). The Users page offered "department", "team" and "role", which the
 * API refuses (#318). Keep these in step with those patterns.
 */
export interface TypeOption {
  readonly value: string;
  readonly label: string;
}

export const OU_TYPES: readonly TypeOption[] = [
  { value: 'country', label: 'Country' },
  { value: 'branch', label: 'Branch' },
  { value: 'division', label: 'Division' },
  { value: 'unit', label: 'Unit' },
  { value: 'section', label: 'Section' },
  { value: 'custom', label: 'Custom' },
];

export const GROUP_TYPES: readonly TypeOption[] = [
  { value: 'access', label: 'Access' },
  { value: 'distribution', label: 'Distribution' },
  { value: 'training', label: 'Training' },
  { value: 'role_based', label: 'Role-based' },
];

/** The API's own default for a new OU. */
export const DEFAULT_OU_TYPE = 'custom';
/** The API's own default for a new security group. */
export const DEFAULT_GROUP_TYPE = 'access';

/**
 * An API refusal as one readable sentence. FastAPI answers a bad field with an array of
 * {loc, msg}; the page used to show that array as "[object Object]". A refused type names
 * the choices the page offers.
 */
export function detailText(err: unknown, fallback: string): string {
  const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d: { loc?: unknown[]; msg?: string }) => {
      const field = String(d?.loc?.[d.loc.length - 1] ?? '');
      if (field === 'ou_type') return `That OU type is not allowed. Choose one of: ${OU_TYPES.map(t => t.label).join(', ')}.`;
      if (field === 'group_type') return `That group type is not allowed. Choose one of: ${GROUP_TYPES.map(t => t.label).join(', ')}.`;
      return field && d?.msg ? `${field}: ${d.msg}` : d?.msg ?? fallback;
    }).join(' ');
  }
  return fallback;
}
