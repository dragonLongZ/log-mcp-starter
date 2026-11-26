/**
 * 开发期自检（Host 半）：用本地假 gateway 验证「服务器配置 + 本地可选字段 → catalog」、
 * 连接状态合并、kubectl 输出解析，以及 gateway 离线时的降级。
 *
 *   cd <project>
 *   node dsh-plugins/log-context/check-host.mjs
 */
import { createServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { internals } from './index.js'

const here = dirname(fileURLToPath(import.meta.url))
const PROJECT_DIR = join(here, '..', '..')
const TOKEN_PATH = join(here, '.token')
const FAKE_PORT = 18765
const FAKE_BASE = `http://127.0.0.1:${FAKE_PORT}`

/** 假 gateway：/targets 故意只给两个资产，其中一个与本地 YAML 同名但内容不同，用来验证「服务器为准」。 */
const server = createServer((req, res) => {
  const send = (status, value) => {
    const body = JSON.stringify(value)
    res.writeHead(status, { 'content-type': 'application/json' })
    res.end(body)
  }
  if (req.method === 'GET' && req.url === '/health') {
    send(200, { assets_total: 2, assets_connected: 1, connected_servers: ['server-172.26.73.42-2.0-prod'] })
    return
  }
  if (req.method === 'GET' && req.url === '/connections') {
    send(200, {
      connections: [
        { server: 'server-172.26.73.42-2.0-prod', state: 'connected', last_heartbeat: '2026-11-14T02:00:00Z', disconnect_reason: null },
        { server: 'server-172-26-73-80-2.0-test', state: 'disconnected', last_heartbeat: null, disconnect_reason: 'SSH connection closed' },
      ],
    })
    return
  }
  if (req.method === 'GET' && req.url === '/targets') {
    send(200, {
      targets: {
        'server-172.26.73.42-2.0-prod': {
          description: 'REMOTE-DESCRIPTION 2.0云平台生产环境',
          modules: { member: { log_dir: '/remote/member', description: '服务器端模块描述' } },
          connection: { state: 'connected' },
        },
        'server-remote-only-1': {
          description: '只存在于服务器端配置的资产',
          modules: {},
          connection: null,
        },
      },
      log_timezone: 'Asia/Shanghai',
    })
    return
  }
  if (req.method === 'POST' && req.url === '/execute-system-query') {
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      const parsed = JSON.parse(body || '{}')
      const stdout = parsed.command.includes('namespaces')
        ? 'namespace/default\nnamespace/parking-prod\nnamespace/kube-system\n'
        : 'pod/cyf-cloud-parking-7d9f-abc\npod/cyf-cloud-cache-5f8b-xyz\n'
      send(200, {
        results: [{ server: parsed.servers[0], executed_command: parsed.command, exit_code: 0, stdout, stderr: '', possibly_truncated_by_bytes: false }],
      })
    })
    return
  }
  send(404, { detail: `unexpected ${req.method} ${req.url}` })
})

await new Promise((resolve) => server.listen(FAKE_PORT, '127.0.0.1', resolve))
console.log(`假 gateway 已监听 ${FAKE_BASE}`)

try {
  const settings = { projectDir: PROJECT_DIR, tokenPath: TOKEN_PATH, gatewayBaseUrl: FAKE_BASE }

  // 1) 在线：服务器 /targets 为准，本地 YAML 补可选字段
  const catalog = await internals.loadCatalog(settings)
  console.log('OK catalog: assets =', catalog.assets.length, '| source =', catalog.catalogSource, '| envs =', catalog.environments.map((e) => e.label).join('/'))
  console.log('OK gateway: reachable =', catalog.gateway.reachable, '| connected =', catalog.gateway.connected, '/', catalog.gateway.total)
  if (catalog.catalogSource !== 'gateway') throw new Error('/targets 可用时应以服务器配置为准')

  const prod = catalog.assets.find((asset) => asset.key === 'server-172.26.73.42-2.0-prod')
  if (!prod) throw new Error('缺少生产资产')
  if (!prod.description.startsWith('REMOTE-DESCRIPTION')) throw new Error('资产描述没有采用服务器端配置')
  if (prod.modules[0]?.logDir !== '/remote/member') throw new Error('模块目录没有采用服务器端配置')
  if (prod.connection?.state !== 'connected') throw new Error('连接状态未合并')
  console.log('OK 服务器为准: description =', prod.description.slice(0, 20), '| module.logDir =', prod.modules[0].logDir, '| state =', prod.connection.state)

  // 本地 YAML 独有的可选字段仍然生效：prod 资产的 env 来自本地 kind/env 推导，K8S 资产来自本地 kind
  const k8sAssets = catalog.assets.filter((asset) => asset.kind === 'k8s').map((asset) => asset.key)
  console.log('OK K8S 资产（本地 kind 生效）:', k8sAssets.join(', '))
  if (k8sAssets.length !== 2) throw new Error('本地 kind: k8s 未生效')

  const remoteOnly = catalog.assets.find((asset) => asset.key === 'server-remote-only-1')
  if (!remoteOnly) throw new Error('服务器端独有资产没有并入')
  console.log('OK 只存在于服务器端的资产已并入:', remoteOnly.key)

  // 2) kubectl 输出解析
  const namespaces = internals.parseKubectlNames('namespace/default\nnamespace/parking-prod\n')
  if (namespaces[0] !== 'default') throw new Error('kubectl 名称解析不正确')
  console.log('OK kubectl 解析:', namespaces.join(','))

  // 3) 离线降级：gateway 不可达时仍用本地 YAML 出目录
  const offline = await internals.loadCatalog({ ...settings, gatewayBaseUrl: 'http://127.0.0.1:18799' })
  console.log('OK 离线降级: assets =', offline.assets.length, '| source =', offline.catalogSource, '| reachable =', offline.gateway.reachable)
  if (offline.gateway.reachable !== false) throw new Error('离线时 reachable 应为 false')
  if (offline.catalogSource !== 'local-yaml') throw new Error('离线时应回退到本地 YAML')
  if (offline.assets.length === 0) throw new Error('离线时不应丢失资产列表')

  // 4) 默认配置：settings.json 优先，其次 patch config，最后默认值
  // 注意：必须传临时设置文件，否则会读到用户真实保存的 ~/.dsh/log-context/settings.json。
  const fs = await import('node:fs')
  const os = await import('node:os')
  const settingsMod = await import('./settings.js')
  const tmpHome = fs.mkdtempSync(join(os.tmpdir(), 'log-context-'))
  const settingsFile = join(tmpHome, 'log-context', 'settings.json')

  const defaults = internals.resolveSettings({}, settingsFile)
  console.log('OK 默认配置: gateway =', defaults.gatewayBaseUrl, '| token 来源 =', defaults.source.token)
  if (defaults.gatewayBaseUrl !== 'http://127.0.0.1:8765') throw new Error('默认网关地址不正确')
  if (defaults.mcpUrl !== 'http://127.0.0.1:8000/mcp') throw new Error('默认 MCP 地址不正确')

  // 4a) 用户保存的设置优先于 patch config（这是「右栏可配」的核心）
  settingsMod.writeSettings(
    { gatewayBaseUrl: FAKE_BASE, mcpUrl: 'http://10.0.0.9:8000/mcp', token: 'tok-from-settings' },
    settingsFile,
  )
  const saved = internals.resolveSettings(
    { gatewayBaseUrl: 'http://should-be-ignored:1', tokenPath: '/nonexistent/.token' },
    settingsFile,
  )
  if (saved.gatewayBaseUrl !== FAKE_BASE) throw new Error('设置文件里的网关地址未优先生效')
  if (saved.mcpUrl !== 'http://10.0.0.9:8000/mcp') throw new Error('设置文件里的 MCP 地址未生效')
  if (saved.token !== 'tok-from-settings') throw new Error('设置文件里的 token 未生效')
  console.log('OK 设置优先:', saved.gatewayBaseUrl, '| mcp =', saved.mcpUrl, '| token 来源 =', saved.source.token)

  // 4b) token 不回明文，只回掩码
  const described = internals.describeSettings(saved)
  if (described.hasToken !== true) throw new Error('hasToken 应为 true')
  if (String(described.tokenMasked).includes('tok-from-settings')) throw new Error('掩码泄露了完整 token')
  if (!described.tokenMasked.includes('***')) throw new Error('掩码格式不正确')
  console.log('OK token 掩码:', described.tokenMasked)

  // 4c) 设置文件损坏时回落到 patch config，不抛错
  fs.writeFileSync(settingsFile, '{ this is not json', 'utf8')
  const broken = internals.resolveSettings({ gatewayBaseUrl: 'http://from-patch:8765' }, settingsFile)
  if (broken.gatewayBaseUrl !== 'http://from-patch:8765') throw new Error('设置损坏时未回落到 patch')
  console.log('OK 设置损坏回落:', broken.gatewayBaseUrl)

  // 4d) URL 校验拒绝非法值
  for (const bad of ['', 'ftp://x', 'not-a-url']) {
    let threw = false
    try {
      settingsMod.normalizeUrl(bad, '网关地址')
    } catch {
      threw = true
    }
    if (!threw) throw new Error(`非法地址未被拒绝：${bad}`)
  }
  console.log('OK URL 校验拒绝非法值')

  // 4e) 别人安装的场景：patch 里没有 projectDir、本地也没有 log_targets.yaml，
  //     但 gateway 可达 —— 不能因此提示「配置文件读不到」。
  settingsMod.writeSettings(
    { gatewayBaseUrl: FAKE_BASE, mcpUrl: 'http://10.0.0.9:8000/mcp', token: 'tok-foreign' },
    settingsFile,
  )
  const foreign = internals.resolveSettings(
    { projectDir: join(tmpHome, 'not-a-repo') },
    settingsFile,
  )
  const foreignCatalog = await internals.loadCatalog(foreign)
  if (foreignCatalog.catalogSource !== 'gateway') throw new Error('gateway 可达时应以它为准')
  if (foreignCatalog.configWarning !== null) {
    throw new Error(`gateway 可用时不应提示本地 YAML 缺失：${foreignCatalog.configWarning}`)
  }
  if (foreignCatalog.assets.length === 0) throw new Error('应能从 gateway 拿到资产')
  console.log('OK 陌生安装目录（无本地 YAML）:', foreignCatalog.assets.length, '资产 | 无多余告警')

  fs.rmSync(tmpHome, { recursive: true, force: true })

  // 5) MCP 状态与手动重连（伪造 tools / loader 服务，不触碰真实运行时）
  const missing = internals.mcpStatus({ get: () => undefined })
  if (missing.known !== false || missing.ready !== false) throw new Error('缺少 tools 服务时应报告 known=false')
  console.log('OK MCP 状态（无 tools 服务）:', JSON.stringify(missing))

  const registered = [{ name: 'mcp__logmcp__list_log_targets' }, { name: 'mcp__logmcp__search_logs' }, { name: 'read' }]
  const fakeTools = { definitions: () => registered }
  const liveStatus = internals.mcpStatus({ get: (name) => (name === 'tools' ? fakeTools : undefined) })
  if (!liveStatus.ready || liveStatus.toolCount !== 2) throw new Error('MCP 工具统计不正确')
  console.log('OK MCP 状态（已注册 2 个）:', liveStatus.ready, liveStatus.toolCount)

  let restarted = 0
  const entry = {
    id: 'include:log-context-mcp',
    options: { id: 'log-context-mcp' },
    fiber: {
      restart: async () => {
        restarted += 1
        registered.push({ name: 'mcp__logmcp__list_log_targets' })
      },
    },
  }
  const fakeLoader = { entries: () => [entry][Symbol.iterator]() }
  const ctxWithLoader = { get: (name) => (name === 'tools' ? fakeTools : name === 'loader' ? fakeLoader : undefined) }
  if (internals.findLoaderEntry(ctxWithLoader, 'log-context-mcp') !== entry) throw new Error('未能按 patch id 找到 Loader 行')
  console.log('OK Loader 行解析到:', entry.id)

  // 模拟「工具已被移除」：清空后手动重连应把工具重新统计到
  registered.length = 0
  if (internals.mcpStatus(ctxWithLoader).ready) throw new Error('清空后不应再报告就绪')
  const restartResult = await internals.restartMcpConnection(ctxWithLoader, 2000)
  if (restarted !== 1) throw new Error('未触发 fiber.restart')
  if (restartResult.readyBefore) throw new Error('重连前状态应为不可用')
  if (!restartResult.readyAfter || restartResult.toolCount !== 1) throw new Error('重连后未统计到恢复的工具')
  console.log('OK 手动重连:', JSON.stringify(restartResult))

  console.log('Host 半全部检查通过')
} finally {
  server.close()
}
