#!/usr/bin/env node
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
// Replace native payloads, never the JS API or a different release's protocol.
import {
    readdirSync,
    readFileSync,
    realpathSync,
    existsSync,
    copyFileSync,
    chmodSync,
    mkdirSync,
    writeFileSync,
} from 'node:fs'
import { join } from 'node:path'

const [store, native, provenanceOutput] = process.argv.slice(2)
if (!store || !native || !provenanceOutput) {
    throw new Error('Expected pnpm store, native build directory, provenance output')
}
const replaced = new Set()
const provenance = new Map()
function replace(path, version) {
    const target = realpathSync(path)
    if (replaced.has(target)) {
        return
    }
    // bin/esbuild can instead be the JS launcher; leave it untouched.
    if (
        !readFileSync(target)
            .subarray(0, 4)
            .equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46]))
    ) {
        return
    }
    const source = join(native, version, 'esbuild')
    const record = JSON.parse(readFileSync(join(native, version, 'provenance.json'), 'utf8'))
    const hash = createHash('sha256').update(readFileSync(source)).digest('hex')
    if (record.version !== version || record.go !== 'go1.27.3' || hash !== record.binarySha256) {
        throw new Error(`Invalid native provenance for esbuild ${version}`)
    }
    if (execFileSync(target, ['--version'], { encoding: 'utf8' }).trim() !== version) {
        throw new Error(`Installed esbuild binary disagrees with package metadata: ${target}`)
    }
    copyFileSync(source, target)
    chmodSync(target, 0o755)
    if (execFileSync(target, ['--version'], { encoding: 'utf8' }).trim() !== version) {
        throw new Error(`Replacement esbuild version mismatch: ${target}`)
    }
    replaced.add(target)
    provenance.set(version, { ...record, buildInfo: readFileSync(join(native, version, 'build-info.txt'), 'utf8') })
}
for (const entry of readdirSync(store)) {
    if (!entry.startsWith('esbuild@') && !entry.startsWith('@esbuild+linux-')) {
        continue
    }
    const modules = join(store, entry, 'node_modules')
    const packages = entry.startsWith('esbuild@')
        ? [join(modules, 'esbuild')]
        : readdirSync(join(modules, '@esbuild')).map((name) => join(modules, '@esbuild', name))
    for (const pkg of packages) {
        const { name, version } = JSON.parse(readFileSync(join(pkg, 'package.json'), 'utf8'))
        if (name !== 'esbuild' && !name.startsWith('@esbuild/linux-')) {
            throw new Error(`Unexpected package ${name}`)
        }
        const binary = join(pkg, 'bin', 'esbuild')
        if (existsSync(binary)) {
            replace(binary, version)
        }
        // esbuild's fallback installer can cache the binary here if optional deps were unavailable.
        const lib = join(pkg, 'lib')
        if (existsSync(lib)) {
            for (const file of readdirSync(lib)) {
                if (file.startsWith('downloaded-') && file.endsWith('-esbuild')) {
                    replace(join(lib, file), version)
                }
            }
        }
    }
}
if (!replaced.size) {
    throw new Error('No installed native esbuild binaries found')
}
mkdirSync(provenanceOutput, { recursive: true })
writeFileSync(join(provenanceOutput, 'esbuild.json'), JSON.stringify([...provenance.values()], null, 2) + '\n')
console.info(
    `Replaced ${replaced.size} native esbuild payloads, retaining JS releases ${[...provenance.keys()].join(', ')}`
)
