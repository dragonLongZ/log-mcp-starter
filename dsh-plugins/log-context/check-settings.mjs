/**
 * 开发期自检（连接设置）：settings.get / settings.test / settings.save 的行为。
 *
 *   cd <project>
 *   node dsh-plugins/log-context/check-settings.mjs
 *
 * 重点验证：
 *   1. 保存后立即生效（不需要重载插件）；
 *   2. token 留空 = 保持不变（不会被掩码值覆盖）；
 *   3. token 永不回明文；
 *   4. 试连不落盘；
 *   5. 保存会重启 MCP 行（否则新地址/新 token 不生效）；
 *   6. 地址非法时拒绝保存，且原文件不被破坏。
 */
import { createServer } from 'node:http'
import { mkdtempSync, rmSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { internals } from './index.js'
import { resolveSettings, readSettings, writeSettings } from './settings.js'

const workdir = mkdtempSync(join(tmpdir(), 'log-context-settings-'))
const settingsFile = join(workdir, 'log-context', 'settings.json')

// ── 假服务：/health 验证网关试连；/mcp 用 SSE 回 initialize 验证 MCP 探活 ──
let healthHits = 0
let mcpHits = 0
let lastAuth = ''
let lastMcpAuth = ''
const server = createServer((req, res) => {
  const send = (status, value) => {
    const body = JSON.stringify(value)
    res.writeHead(status, { 'content-type': 'application/json' })
    res.end(body)
  }
  if (req.method === 'GET' && req.url === '/health') {
    healthHits += 1
    lastAuth = String(req.headers.authorization || '')
    send(200, { assets_total: 3, assets_connected: 1, connected_servers: ['a'] })
    return
  }
  if (req.method === 'POST' && req.url === '/mcp') {
    mcpHits += 1
    lastMcpAuth = String(req.headers.authorization || '')
    if (lastMcpAuth !== 'Bearer candidate-token' && lastMcpAuth !== 'Bearer tok-first-123456') {
      send(401, { error: 'Invalid bearer token' })
      return
    }
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      const parsed = JSON.parse(body || '{}')
      const payload = JSON.stringify({
        jsonrpc: '2.0',
        id: parsed.id ?? 1,
        result: {
          protocolVersion: '2024-11-05',
          capabilities: {},
          serverInfo: { name: 'JumpServer Multi-asset Log MCP', version: '0.1.0' },
        },
      })
      res.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-store',
      })
      res.end(`event: message\ndata: ${payload}\n\n`)
    })
    return
  }
  send(404, { detail: `unexpected ${req.method} ${req.url}` })
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port
const BASE = `http://127.0.0.1:${port}`
const MCP_URL = `${BASE}/mcp`

/** 造一个最小的 ctx：tools / loader 都返回可控假实现。 */
function makeCtx() {
  const registered = [{ name: 'mcp__logmcp__list_log_targets' }]
  let restarts = 0
  const entry = {
    id: 'include:log-context-mcp',
    options: { id: 'log-context-mcp' },
    fiber: {
      restart: async () => {
        restarts += 1
      },
    },
  }
  const services = {
    tools: { definitions: () => registered },
    loader: { entries: () => [entry][Symbol.iterator]() },
  }
  return {
    ctx: { get: (name) => services[name] },
    restarts: () => restarts,
    tools: () => registered,
  }
}

try {
  // projectDir 指向临时目录：确保「未设置 token」时不会读到仓库里的 local.token
  const patchConfig = { projectDir: workdir, gatewayBaseUrl: 'http://from-patch:8765' }
  const getSettings = () => internals.resolveSettings(patchConfig, settingsFile)
  const { ctx, restarts } = makeCtx()
  const dispatch = internals.createDispatcher(ctx, getSettings)

  // ── 1. 初始：没有设置文件，回落到 patch ──
  const initial = await dispatch['settings.get']({})
  if (initial.gatewayBaseUrl !== 'http://from-patch:8765') throw new Error('初始地址应来自 patch')
  if (initial.hasToken !== false) throw new Error('尚未设置 token 时 hasToken 应为 false')
  if (existsSync(settingsFile)) throw new Error('只读不应创建设置文件')
  console.log('OK 初始状态:', initial.gatewayBaseUrl, '| hasToken =', initial.hasToken)

  // ── 2. token 为空时拒绝保存 ──
  let rejected = false
  try {
    await dispatch['settings.save']({ gatewayBaseUrl: BASE, mcpUrl: 'http://x:1/mcp', token: '' })
  } catch (error) {
    rejected = error.code === 'bad-request'
  }
  if (!rejected) throw new Error('空 token 应被拒绝（且 code=bad-request）')
  if (existsSync(settingsFile)) throw new Error('保存失败不应写出文件')
  console.log('OK 空 token 被拒绝，未落盘')

  // ── 3. 首次保存：写文件 + 重启 MCP 行 ──
  const saved1 = await dispatch['settings.save']({
    gatewayBaseUrl: `${BASE}/`,
    mcpUrl: 'http://mcp.example:8000/mcp',
    token: 'tok-first-123456',
  })
  if (!existsSync(settingsFile)) throw new Error('保存后应写出设置文件')
  if (saved1.gatewayBaseUrl !== BASE) throw new Error('末尾斜杠应被规范化')
  if (saved1.hasToken !== true) throw new Error('保存后 hasToken 应为 true')
  if (saved1.mcp === null || saved1.mcp === undefined) throw new Error('保存应返回 MCP 重连结果')
  if (restarts() !== 1) throw new Error('保存后应重启 MCP 行一次（否则新地址不生效）')
  console.log('OK 首次保存:', saved1.gatewayBaseUrl, '| 重启次数 =', restarts())

  // ── 4. token 不回明文 ──
  if (JSON.stringify(saved1).includes('tok-first-123456')) throw new Error('返回值泄露了完整 token')
  console.log('OK 返回值不含明文 token:', saved1.tokenMasked)

  // ── 5. 留空 token = 保持不变 ──
  const saved2 = await dispatch['settings.save']({
    gatewayBaseUrl: 'http://changed:8765',
    mcpUrl: 'http://mcp2.example:8000/mcp',
    token: '',
  })
  const onDisk = readSettings(settingsFile)
  if (onDisk.token !== 'tok-first-123456') throw new Error('留空 token 时不应覆盖原 token')
  if (onDisk.gatewayBaseUrl !== 'http://changed:8765') throw new Error('地址应已更新')
  if (saved2.gatewayBaseUrl !== 'http://changed:8765') throw new Error('保存后应立即可读到新地址')
  console.log('OK 留空 token 保持不变，地址已更新为', saved2.gatewayBaseUrl)

  // ── 6. 非法地址被拒绝，原文件不被破坏 ──
  for (const bad of ['ftp://x', 'not-a-url', '']) {
    let threw = false
    try {
      await dispatch['settings.save']({ gatewayBaseUrl: bad, mcpUrl: 'http://x:1/mcp', token: 'x' })
    } catch {
      threw = true
    }
    if (!threw) throw new Error(`非法网关地址未被拒绝：${bad}`)
  }
  if (readSettings(settingsFile).gatewayBaseUrl !== 'http://changed:8765') {
    throw new Error('保存失败破坏了已有设置')
  }
  console.log('OK 非法地址被拒绝，原设置完好')

  // ── 7. 试连：用表单值（不落盘），gateway 与 MCP 都要测 ──
  writeSettings({ gatewayBaseUrl: 'http://127.0.0.1:1', mcpUrl: 'http://x:1/mcp', token: 'old-token' }, settingsFile)
  const beforeTest = healthHits
  const beforeMcp = mcpHits
  const testResult = await dispatch['settings.test']({
    gatewayBaseUrl: BASE,
    mcpUrl: MCP_URL,
    token: 'candidate-token',
  })
  if (!testResult.ok) throw new Error(`试连应成功：${testResult.message}`)
  if (healthHits !== beforeTest + 1) throw new Error('试连应实际请求 /health')
  if (mcpHits !== beforeMcp + 1) throw new Error('试连应实际请求 MCP initialize')
  if (lastAuth !== 'Bearer candidate-token') throw new Error('试连应使用表单里的 token')
  if (lastMcpAuth !== 'Bearer candidate-token') throw new Error('MCP 探活应使用表单里的 token')
  if (readSettings(settingsFile).token !== 'old-token') throw new Error('试连不应改动设置文件')
  if (!testResult.gateway.message.includes('3 个资产')) throw new Error('网关试连结果应包含资产数')
  if (!testResult.mcp.message.includes('握手成功')) throw new Error('MCP 试连应报告握手成功')
  console.log('OK 试连不落盘:', testResult.message)

  // ── 7a. MCP 地址填错时必须报错（本次线上问题的回归）──
  const wrongMcp = await dispatch['settings.test']({
    gatewayBaseUrl: BASE,
    mcpUrl: 'http://127.0.0.1:1/mcp',
    token: 'candidate-token',
  })
  if (wrongMcp.ok !== false) throw new Error('MCP 地址不可达时整体应为失败')
  if (wrongMcp.gateway.ok !== true) throw new Error('网关部分应仍是成功的')
  if (wrongMcp.mcp.ok !== false) throw new Error('MCP 部分应失败')
  if (!wrongMcp.mcp.message.includes('无法连接 MCP')) throw new Error('应明确指出 MCP 连不上')
  console.log('OK MCP 地址填错能被发现:', wrongMcp.mcp.message.slice(0, 50))

  // ── 7b. MCP token 不对时报 401 ──
  const badToken = await dispatch['settings.test']({
    gatewayBaseUrl: BASE,
    mcpUrl: MCP_URL,
    token: 'wrong-token',
  })
  if (badToken.mcp.ok !== false) throw new Error('token 不对时 MCP 应失败')
  if (!/401|鉴权/.test(badToken.mcp.message)) throw new Error('应提示鉴权失败')
  console.log('OK MCP token 不对可识别:', badToken.mcp.message.slice(0, 40))

  // ── 8. 试连失败：返回 ok=false 而不是抛错，便于面板展示 ──
  const failed = await dispatch['settings.test']({ gatewayBaseUrl: 'http://127.0.0.1:1', token: 'x' })
  if (failed.ok !== false) throw new Error('不可达时应返回 ok=false')
  if (!failed.message) throw new Error('失败时应给出原因')
  console.log('OK 试连失败可展示:', failed.message.slice(0, 40))

  // ── 8a. mcp.probe 单独可用（面板「测试端点」按钮）──
  writeSettings({ gatewayBaseUrl: BASE, mcpUrl: MCP_URL, token: 'tok-first-123456' }, settingsFile)
  const probe = await dispatch['mcp.probe']({})
  if (probe.ok !== true) throw new Error(`端点探活应成功：${probe.message}`)
  if (!probe.message.includes('JumpServer Multi-asset Log MCP')) throw new Error('应回显服务名')
  console.log('OK 端点探活:', probe.message)

  // ── 9. 设置文件损坏时 catalog 仍能出目录（回落到 patch + 本地 YAML）──
  writeFileSync(settingsFile, '{broken', 'utf8')
  const fallback = internals.resolveSettings(patchConfig, settingsFile)
  if (fallback.gatewayBaseUrl !== 'http://from-patch:8765') throw new Error('损坏时应回落到 patch')
  const reloaded = resolveSettings(patchConfig, { file: settingsFile })
  if (reloaded.gatewayBaseUrl !== 'http://from-patch:8765') throw new Error('resolveSettings 应容错')
  console.log('OK 设置文件损坏可降级')

  console.log('连接设置检查通过')
} finally {
  server.close()
  rmSync(workdir, { recursive: true, force: true })
}
