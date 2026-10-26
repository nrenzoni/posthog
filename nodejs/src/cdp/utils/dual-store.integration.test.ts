import Redis from 'ioredis'
import { randomUUID } from 'node:crypto'

import { configureValkeyReads, dualRead, dualWrite } from './dual-store'

// Uses the primary CDP endpoint, which may itself be Valkey. No shadow connection is opened.
describe('single-store CDP commands on Redis/Valkey', () => {
    let primary: Redis.Redis
    let key: string

    beforeAll(async () => {
        primary = new Redis(Number(process.env.CDP_REDIS_PORT ?? 6379), process.env.CDP_REDIS_HOST || '127.0.0.1', {
            password: process.env.CDP_REDIS_PASSWORD || undefined,
            maxRetriesPerRequest: 1,
            lazyConnect: true,
        })
        await primary.connect()
    })

    beforeEach(() => {
        key = `@posthog-test/single-store/${randomUUID()}`
        configureValkeyReads('', false)
    })

    afterEach(async () => {
        configureValkeyReads('')
        await primary.del(key)
    })

    afterAll(async () => {
        await primary.quit()
    })

    it('runs INCR exactly once for writes and mutating reads, without a secondary', async () => {
        const increment = jest.fn(() => primary.incr(key))
        const secondary = jest.fn(() => Promise.reject(new Error('secondary must not run')))

        await dualWrite('hog-watcher.observeResults', increment, secondary)
        expect(await primary.get(key)).toBe('1')
        expect(increment).toHaveBeenCalledTimes(1)
        expect(secondary).not.toHaveBeenCalled()

        await expect(dualRead('hog-masker.filterByMasking', increment, secondary)).resolves.toBe(2)
        expect(await primary.get(key)).toBe('2')
        expect(increment).toHaveBeenCalledTimes(2)
        expect(secondary).not.toHaveBeenCalled()
    })

    it('propagates a real primary command failure without attempting the secondary', async () => {
        await primary.set(key, 'not-an-integer')
        const secondary = jest.fn().mockResolvedValue(undefined)
        await expect(dualWrite('hog-watcher.observeResults', () => primary.incr(key), secondary)).rejects.toThrow(
            /integer/
        )
        expect(await primary.get(key)).toBe('not-an-integer')
        expect(secondary).not.toHaveBeenCalled()
    })
})
