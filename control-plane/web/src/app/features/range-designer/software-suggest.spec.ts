import { SoftwareCatalogue } from '@core/services/api.service';
import {
  installWarning, joinServices, osFamilyOf, parseServices, suggestServices,
} from './software-suggest';

const CATALOGUE: SoftwareCatalogue = {
  software: [
    { name: '7zip', aliases: ['7-zip'], os_families: ['windows', 'linux'] },
    { name: 'googlechrome', aliases: ['chrome', 'google-chrome'], os_families: ['windows'] },
    { name: 'nginx', aliases: [], os_families: ['linux'] },
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

  describe('storage format', () => {
    it('round-trips the comma-separated nodeData.services string', () => {
      expect(parseServices('dns,7zip,,nginx')).toEqual(['dns', '7zip', 'nginx']);
      expect(parseServices(' git , vlc ')).toEqual(['git', 'vlc']);
      expect(parseServices(undefined)).toEqual([]);
      expect(joinServices(['dns', '7zip'])).toBe('dns,7zip');
    });
  });
});
