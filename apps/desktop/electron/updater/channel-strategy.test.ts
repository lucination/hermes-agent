import { expect, test, vi } from 'vitest'

import type { ChannelTarget } from './channel'
import { ChannelStrategy } from './channel-strategy'

import type { UpdaterStatusWire } from './index'

test.each([null, undefined])(
  'unknown native availability (%s) invalidates a previously available selection',
  async (availability): Promise<void> => {
    const identity = {
      token: 'a'.repeat(16),
      displayName: 'Preview',
      appId: 'chat.nous.preview',
      appNamePascal: 'Preview',
      artifactNamePascal: 'Preview',
      cliName: 'preview',
      windowsExecutableName: 'preview',
      msixAppIdWithOrg: 'NousResearch.Preview'
    }

    const target: ChannelTarget = {
      channel: {
        schema: 1,
        state: 'active',
        name: 'preview',
        repository: 'NousResearch/hermes-agent',
        policy: 'preview',
        revision: 1,
        nextSequence: 2,
        head: null,
        identity
      },
      manifest: {
        schema: 1,
        packages: [],
        request: {
          schema: 1,
          buildId: 'b'.repeat(32),
          channel: 'preview',
          sequence: 1,
          repository: 'NousResearch/hermes-agent',
          commit: 'c'.repeat(40),
          sourceVersion: '1.0.0',
          version: '0.0.1',
          windowsVersion: '0.0.1.0',
          identity,
          bundleEnv: {},
          publicBase: 'https://example.com'
        }
      },
      package: {
        platform: 'win32',
        arch: 'x64',
        variant: 'bundled',
        version: '0.0.1.0',
        identity: identity.msixAppIdWithOrg,
        publisher: 'CN=Test',
        artifact: {
          key: 'stable.msixbundle',
          sha256: 'e'.repeat(64),
          size: 1
        },
        feed: { key: 'stable.appinstaller', channel: 'stable' }
      },
      artifactUrl: 'https://example.com/stable.msixbundle',
      feedUrl: 'https://example.com/stable.appinstaller',
      manifestSha256: 'd'.repeat(64)
    }

    const nativeStatus: UpdaterStatusWire = { supported: true, updateAvailable: true }
    const apply = vi.fn(async () => ({ ok: true }))

    const strategy = new ChannelStrategy({
      resolver: { resolve: async () => ({ kind: 'active', target }) },
      build: { ...target.manifest.request, sequence: 0 },
      mechanism: 'app-installer',
      nativeFactory: () => ({
        mechanism: 'app-installer',
        check: async () => nativeStatus,
        apply
      })
    })

    expect((await strategy.check()).updateAvailable).toBe(true)
    nativeStatus.updateAvailable = availability
    await expect(strategy.check()).rejects.toThrow('Native update availability unknown')
    await expect(strategy.apply()).rejects.toThrow('Native update availability unknown')
    expect(apply).not.toHaveBeenCalled()
  }
)
