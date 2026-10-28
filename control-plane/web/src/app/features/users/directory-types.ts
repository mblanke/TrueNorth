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
