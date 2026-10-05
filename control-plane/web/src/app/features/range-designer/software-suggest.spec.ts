import { SoftwareCatalogue } from '@core/services/api.service';
import {
  chipWarning, installWarning, joinServices, offlineWarning, osFamilyOf, parseServices, suggestServices,
} from './software-suggest';

const CATALOGUE: SoftwareCatalogue = {
  software: [
    { name: '7zip', aliases: ['7-zip'], os_families: ['windows', 'linux'], offline: true },
    // A community wrapper that downloads its installer at install time.
    { name: 'googlechrome', aliases: ['chrome', 'google-chrome'], os_families: ['windows'], offline: false },
    { name: 'nginx', aliases: [], os_families: ['linux'], offline: true },
    // No `offline` (an older API): never warned about.
    { name: 'sysinternals', aliases: [], os_families: ['windows'] },
  ],
  roles: ['dns', 'iis'],
};

const values = (xs: { value: string }[]) => xs.map(x => x.value);

describe('software-suggest', () => {
  describe('osFamilyOf', () => {
    it('follows the worker: win -> windows, appliances, else linux', () => {
      expect(osFamilyOf('windows-11')).toBe('windows');
      expect(osFamilyOf('win11-analyst')).toBe('windows');
      expect(osFamilyOf('ubuntu-24.04')).toBe('linux');
      expect(osFamilyOf('rocky-9')).toBe('linux');
      expect(osFamilyOf('pfsense')).toBe('appliance');
      expect(osFamilyOf('vyos')).toBe('appliance');
      expect(osFamilyOf('ubuntu-24.04', 'router')).toBe('appliance');
      expect(osFamilyOf('')).toBeNull();
    });
  });

  describe('suggestServices', () => {
    it('offers only software the node family can install, plus roles', () => {
      expect(values(suggestServices(CATALOGUE, 'linux', '', [])))
        .toEqual(['7zip', 'nginx', 'dns', 'iis']);
      expect(values(suggestServices(CATALOGUE, 'windows', '', [])))
        .toEqual(['7zip', 'googlechrome', 'sysinternals', 'dns', 'iis']);
    });

    it('offers roles only to appliances, and everything while the OS is unset', () => {
      expect(values(suggestServices(CATALOGUE, 'appliance', '', []))).toEqual(['dns', 'iis']);
      expect(values(suggestServices(CATALOGUE, null, '', [])).length).toBe(6);
    });

    it('matches the query against names and aliases, case-insensitively', () => {
      expect(values(suggestServices(CATALOGUE, 'windows', 'Chrome', []))).toEqual(['googlechrome']);
      expect(values(suggestServices(CATALOGUE, 'windows', '7-z', []))).toEqual(['7zip']);
      expect(values(suggestServices(CATALOGUE, 'linux', 'chrome', []))).toEqual([]);
      expect(values(suggestServices(CATALOGUE, 'linux', 'dn', []))).toEqual(['dns']);
    });

    it('leaves out what the node already has, by name or alias', () => {
      expect(values(suggestServices(CATALOGUE, 'windows', '', ['chrome', 'DNS', '7zip'])))
        .toEqual(['sysinternals', 'iis']);
    });

    it('offers nothing without a catalogue (free text still works)', () => {
      expect(suggestServices(null, 'linux', 'ng', [])).toEqual([]);
    });
  });

  describe('installWarning', () => {
    it('flags software the family cannot install and unknown names', () => {
      expect(installWarning(CATALOGUE, 'linux', 'googlechrome')).toContain('No linux install');
      expect(installWarning(CATALOGUE, 'linux', 'nginx')).toBeNull();
      expect(installWarning(CATALOGUE, 'windows', 'chrome')).toBeNull();
      expect(installWarning(CATALOGUE, 'linux', 'dns')).toBeNull();
      expect(installWarning(CATALOGUE, 'linux', 'frobnicator')).toContain('Not in the software catalogue');
      expect(installWarning(CATALOGUE, 'appliance', '7zip')).toContain('Appliances');
      expect(installWarning(null, 'linux', 'anything')).toBeNull();
    });
  });

  describe('offlineWarning', () => {
    it('flags Windows software that is not offline-ready, by name or alias', () => {
      expect(offlineWarning(CATALOGUE, 'windows', 'googlechrome')).toContain('Not offline-ready');
      expect(offlineWarning(CATALOGUE, 'windows', 'Chrome')).toContain('Not offline-ready');
    });

    it('is quiet for offline-ready, unflagged, non-Windows, unknown and role chips', () => {
      expect(offlineWarning(CATALOGUE, 'windows', '7zip')).toBeNull();
      expect(offlineWarning(CATALOGUE, 'windows', 'sysinternals')).toBeNull();
      expect(offlineWarning(CATALOGUE, 'linux', 'googlechrome')).toBeNull();
      expect(offlineWarning(CATALOGUE, null, 'googlechrome')).toBeNull();
      expect(offlineWarning(CATALOGUE, 'windows', 'frobnicator')).toBeNull();
      expect(offlineWarning(CATALOGUE, 'windows', 'dns')).toBeNull();
      expect(offlineWarning(null, 'windows', 'googlechrome')).toBeNull();
    });

    it('chipWarning puts a blocking problem first, then the offline one', () => {
      expect(chipWarning(CATALOGUE, 'windows', 'googlechrome')).toContain('Not offline-ready');
      expect(chipWarning(CATALOGUE, 'linux', 'googlechrome')).toContain('No linux install');
      expect(chipWarning(CATALOGUE, 'windows', 'frobnicator')).toContain('Not in the software catalogue');
      expect(chipWarning(CATALOGUE, 'windows', '7zip')).toBeNull();
    });
  });

  describe('storage format', () => {
    it('round-trips the comma-separated nodeData.services string', () => {
      expect(parseServices('dns,7zip,,nginx')).toEqual(['dns', '7zip', 'nginx']);
      expect(parseServices(' git , vlc ')).toEqual(['git', 'vlc']);
      expect(parseServices(undefined)).toEqual([]);
      expect(joinServices(['dns', '7zip'])).toBe('dns,7zip');
    });
  });
});
