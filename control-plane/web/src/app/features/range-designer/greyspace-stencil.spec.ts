import { GREYSPACE_DEFAULTS, GREYSPACE_KEYS, greyspaceBlock, greyspaceSettings } from './greyspace-stencil';

describe('greyspace-stencil', () => {
  it('fills defaults and keeps only block keys', () => {
    const s = greyspaceSettings({ corpus_tier: 't2', bogus: 1, network: 'inet' });
    expect(s).toEqual({ ...GREYSPACE_DEFAULTS, corpus_tier: 't2', network: 'inet' });
    expect(greyspaceSettings(null)).toEqual(GREYSPACE_DEFAULTS);
    expect(greyspaceSettings(['not', 'a', 'mapping'])).toEqual(GREYSPACE_DEFAULTS);
  });

  it('exports the block with the template keys only', () => {
    const block = greyspaceBlock({ ...GREYSPACE_DEFAULTS, npc_profile: 'office-day' });
    expect(Object.keys(block!).sort()).toEqual(GREYSPACE_KEYS.filter(k => k !== 'public_prefix').sort());
    expect(block!['npc_profile']).toBe('office-day');
  });

  it('a cloud with Greyspace switched off is only a drawing', () => {
    expect(greyspaceBlock({ ...GREYSPACE_DEFAULTS, enabled: false })).toBeNull();
    expect(greyspaceBlock(null)).toBeNull();
    expect(greyspaceSettings({ enabled: false }).enabled).toBeFalse();
  });
});
