/**
 * Host 半：把 log-mcp-starter 的资产配置变成 DSH 可读的「日志上下文」。
 *
 * 职责只有三件事，都不碰 Python 侧业务逻辑：
 * 1. 读取 `config/log_targets.yaml`（支持 kind/env/k8s 等可选字段，缺省时从 description 推断），
 *    提供 `/log-context/api` 同源接口给右栏面板，附带 gateway 的连接状态。
 * 2. 代理两个只读/重连动作到本地 gateway（`/connect`、`/execute-system-query`），
 *    供面板里的「重连」「拉取命名空间 / Pod」使用。
 * 3. 注册一段系统提示，告诉模型看到「【日志上下文】」块就直接调用 `mcp__logmcp__*` 工具。
 *
 * MCP 连接本身由本 bundle 的 cordis.patch.yml 插入的 `@deepseek-ai/dsh-mcp-client` 行负责。
 */
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Loader 行 id 与展示名。 */
export const name = 'log-context'

/** 依赖的 Host 服务：HTTP 路由、请求信任检查、系统提示。 */
export const inject = ['webServer', 'connection', 'systemPrompt']

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
/** bundle 位于 `<project>/dsh-plugins/log-context/`，因此项目根是上两级。 */
const DEFAULT_PROJECT_DIR = resolve(PLUGIN_DIR, '..', '..')
const API_PATH = '/log-context/api'
const DEFAULT_CONFIG_FILE = 'config/log_targets.yaml'
const DEFAULT_TOKEN_FILE = 'local.token'
const DEFAULT_GATEWAY_PORT = 8765
const DEFAULT_MCP_PORT = 8000
const GATEWAY_TIMEOUT_MS = 3000
const MAX_BODY_BYTES = 1 << 20

// ─────────────────────────── 极简 YAML 解析 ───────────────────────────
// 只服务于 log_targets.yaml 这种「映射 + 标量 + 少量列表」的配置；不追求完整 YAML。

/** 去掉行尾注释（引号内的 # 不处理）。 */
function stripInlineComment(content) {
  let inSingle = false
  let inDouble = false
  for (let i = 0; i < content.length; i += 1) {
    const char = content[i]
    if (char === "'" && !inDouble) inSingle = !inSingle
    else if (char === '"' && !inSingle) inDouble = !inDouble
    else if (char === '#' && !inSingle && !inDouble) {
      if (i === 0 || content[i - 1] === ' ' || content[i - 1] === '\t') {
        return content.slice(0, i).trimEnd()
      }
    }
  }
  return content.trimEnd()
}

/** 把 YAML 文本切成带缩进的有效行。 */
function tokenizeYaml(text) {
  const lines = []
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trimEnd()
    if (!line.trim()) continue
    if (line.trimStart().startsWith('#')) continue
    const indent = line.length - line.trimStart().length
    const content = stripInlineComment(line.slice(indent))
    if (!content) continue
    lines.push({ indent, content })
  }
  return lines
}

/** 解析一个标量（含空列表、引号字符串、数字、布尔）。 */
function parseScalar(raw) {
  const value = raw.trim()
  if (value === '') return ''
  if (value === '[]') return []
  if (value === '{}') return {}
  if (value === 'null' || value === '~') return null
  if (value === 'true') return true
  if (value === 'false') return false
  if (/^[+-]?\d+$/.test(value)) return Number(value)
  if (/^[+-]?(?:\d+\.\d*|\.\d+)$/.test(value)) return Number(value)
  if (value.startsWith('"') && value.endsWith('"') && value.length > 1) {
    try {
      return JSON.parse(value)
    } catch {
      return value.slice(1, -1)
    }
  }
  if (value.startsWith("'") && value.endsWith("'") && value.length > 1) {
    return value.slice(1, -1).replace(/''/g, "'")
  }
  if (value.startsWith('[') && value.endsWith(']')) {
    const inner = value.slice(1, -1).trim()
    if (!inner) return []
    return splitInlineItems(inner).map(parseScalar)
  }
  return value
}

/** 按顶层逗号切分内联列表（引号内的逗号保留）。 */
function splitInlineItems(inner) {
  const parts = []
  let current = ''
  let inSingle = false
  let inDouble = false
  for (const char of inner) {
    if (char === "'" && !inDouble) inSingle = !inSingle
    else if (char === '"' && !inSingle) inDouble = !inDouble
    if (char === ',' && !inSingle && !inDouble) {
      parts.push(current)
      current = ''
      continue
    }
    current += char
  }
  parts.push(current)
  return parts.map((part) => part.trim()).filter(Boolean)
}

/** 解析键值行；返回 null 表示不是映射行。 */
function matchMappingLine(content) {
  if (content.startsWith('- ') || content === '-') return null
  const index = content.indexOf(':')
  if (index <= 0) return null
  const key = content.slice(0, index).trim()
  const rest = content.slice(index + 1).trim()
  if (!key) return null
  return { key, rest }
}

/** 解析缩进块（映射或列表）。 */
function parseBlock(lines, start, indent) {
  const first = lines[start]
  if (!first) return [null, start]
  if (first.content === '-' || first.content.startsWith('- ')) return parseSequence(lines, start, indent)
  return parseMapping(lines, start, indent)
}

function parseMapping(lines, start, indent) {
  const result = {}
  let index = start
  while (index < lines.length) {
    const line = lines[index]
    if (line.indent !== indent) break
    const entry = matchMappingLine(line.content)
    if (!entry) break
    if (entry.rest === '') {
      const next = lines[index + 1]
      if (next && next.indent > indent) {
        const [value, nextIndex] = parseBlock(lines, index + 1, next.indent)
        result[entry.key] = value
        index = nextIndex
      } else {
        result[entry.key] = null
        index += 1
      }
    } else {
      result[entry.key] = parseScalar(entry.rest)
      index += 1
    }
  }
  return [result, index]
}

function parseSequence(lines, start, indent) {
  const result = []
  let index = start
  while (index < lines.length) {
    const line = lines[index]
    if (line.indent !== indent) break
    if (line.content !== '-' && !line.content.startsWith('- ')) break
    const rest = line.content === '-' ? '' : line.content.slice(2).trim()
    if (rest === '') {
      const next = lines[index + 1]
      if (next && next.indent > indent) {
        const [value, nextIndex] = parseBlock(lines, index + 1, next.indent)
        result.push(value)
        index = nextIndex
      } else {
        result.push(null)
        index += 1
      }
      continue
    }
    const entry = matchMappingLine(rest)
    if (entry) {
      // 列表项本身是映射（`- key: value`）：以该项为缩进取一个映射块。
      const synthetic = [{ indent: indent + 2, content: rest }]
      const restLines = []
      let cursor = index + 1
      while (cursor < lines.length && lines[cursor].indent > indent) {
        restLines.push(lines[cursor])
        cursor += 1
      }
      const [value] = parseMapping(synthetic.concat(restLines), 0, indent + 2)
      result.push(value)
      index = cursor
      continue
    }
    result.push(parseScalar(rest))
    index += 1
  }
  return [result, index]
}

/** 解析整份 YAML 文本。 */
function parseYaml(text) {
  const lines = tokenizeYaml(text)
  if (lines.length === 0) return {}
  const [value, index] = parseBlock(lines, 0, lines[0].indent)
  return index <= 0 ? {} : value
}

// ─────────────────────────── 配置与目录派生 ───────────────────────────

/** 读取并解析一次配置文件；失败时抛出可读错误。 */
function loadTargets(projectDir) {
  const configFile = join(projectDir, DEFAULT_CONFIG_FILE)
  let text
  try {
    text = readFileSync(configFile, 'utf8')
  } catch (error) {
    const failure = new Error(`无法读取配置文件 ${configFile}：${error.message}`)
    failure.code = 'config-unreadable'
    throw failure
  }
  let parsed
  try {
    parsed = parseYaml(text)
  } catch (error) {
    const failure = new Error(`解析 ${configFile} 失败：${error.message}`)
    failure.code = 'config-invalid'
    throw failure
  }
  if (!parsed || typeof parsed !== 'object' || !parsed.targets || typeof parsed.targets !== 'object') {
    const failure = new Error(`${configFile} 中缺少 targets 段`)
    failure.code = 'config-invalid'
    throw failure
  }
  return { configFile, raw: parsed }
}

const ENV_RULES = [
  { id: 'prod', label: '生产', pattern: /生产|prod/i },
  { id: 'test', label: '测试', pattern: /测试|test/i },
  { id: 'dev', label: '研发', pattern: /研发|开发|dev/i },
  { id: 'gateway', label: '网关', pattern: /网关|gateway/i },
  { id: 'demo', label: '演示', pattern: /演示|demo/i },
]
const ENV_ORDER = ['prod', 'test', 'dev', 'gateway', 'demo', 'other']

const PLATFORM_RULES = [
  { id: '2.0', pattern: /2\.0/ },
  { id: '3.0', pattern: /3\.0/ },
  { id: '佳木斯', pattern: /佳木斯/ },
  { id: '雄安', pattern: /雄安/ },
  { id: '北菜岗亭', pattern: /北菜|岗亭/ },
]

/** 从 description 推断环境；显式 env 优先，并归一到同一个分类 id。 */
function inferEnv(target) {
  const explicit = typeof target.env === 'string' ? target.env.trim() : ''
  if (explicit) {
    const rule = ENV_RULES.find(
      (item) => item.label === explicit || item.id === explicit || item.pattern.test(explicit),
    )
    if (rule) return { id: rule.id, label: rule.label }
    return { id: explicit, label: explicit }
  }
  const description = String(target.description ?? '')
  for (const rule of ENV_RULES) {
    if (rule.pattern.test(description)) return { id: rule.id, label: rule.label }
  }
  return { id: 'other', label: '未分类' }
}

/** 从 description 提取平台标签，仅用于展示。 */
function inferTags(target) {
  const description = String(target.description ?? '')
  return PLATFORM_RULES.filter((rule) => rule.pattern.test(description)).map((rule) => rule.id)
}

/** 一个资产是 K8S 集群还是普通 Linux 资产。 */
function inferKind(target) {
  if (typeof target.kind === 'string' && target.kind.trim()) {
    return target.kind.trim() === 'k8s' ? 'k8s' : 'linux'
  }
  return /k8s|kubernetes/i.test(String(target.description ?? '')) ? 'k8s' : 'linux'
}

/**
 * 把合并后的资产条目整理成面板需要的目录结构。
 * @param entries - `[{ key, description, modules, connection, optional }]`。
 *   `modules` / `connection` 优先取 gateway 的实时配置（服务器为准），
 *   `optional` 是本地 YAML 里的展示字段（env / kind / k8s）。
 * @returns `{ assets, environments }`。
 */
function buildCatalog(entries) {
  const assets = []
  for (const entry of entries) {
    const optional = entry.optional && typeof entry.optional === 'object' ? entry.optional : {}
    const description = String(entry.description ?? '')
    const env = inferEnv({ description, env: optional.env })
    const modules = Object.entries(entry.modules ?? {}).map(([moduleName, moduleConfig]) => ({
      name: moduleName,
      description: String(moduleConfig?.description ?? ''),
      logDir: String(moduleConfig?.log_dir ?? ''),
    }))
    const k8sConfig = optional.k8s && typeof optional.k8s === 'object' ? optional.k8s : {}
    const connection = entry.connection
    assets.push({
      key: entry.key,
      name: entry.key,
      description,
      envId: env.id,
      envLabel: env.label,
      tags: inferTags({ description }),
      kind: inferKind({ description, kind: optional.kind }),
      modules,
      k8s: {
        context: String(k8sConfig.context ?? ''),
        namespaces: Array.isArray(k8sConfig.namespaces) ? k8sConfig.namespaces.map(String) : [],
      },
      connection: connection
        ? {
            state: String(connection.state ?? 'unknown'),
            lastHeartbeat: connection.last_heartbeat ?? null,
            disconnectReason: connection.disconnect_reason ?? null,
          }
        : null,
    })
  }
  assets.sort((left, right) => {
    const leftOrder = ENV_ORDER.indexOf(left.envId)
    const rightOrder = ENV_ORDER.indexOf(right.envId)
    const leftRank = leftOrder === -1 ? ENV_ORDER.length : leftOrder
    const rightRank = rightOrder === -1 ? ENV_ORDER.length : rightOrder
    if (leftRank !== rightRank) return leftRank - rightRank
    return left.key.localeCompare(right.key)
  })
  const environments = []
  const environmentLabels = new Map()
  for (const asset of assets) environmentLabels.set(asset.envId, asset.envLabel)
  for (const id of ENV_ORDER) {
    if (!environmentLabels.has(id)) continue
    environments.push({ id, label: environmentLabels.get(id), count: assets.filter((asset) => asset.envId === id).length })
  }
  for (const [id, label] of environmentLabels) {
    if (ENV_ORDER.includes(id)) continue
    environments.push({ id, label, count: assets.filter((asset) => asset.envId === id).length })
  }
  return { assets, environments }
}

/** 解析运行参数，全部来自 bundle 行 config，缺省值指向当前项目。 */
function resolveSettings(config) {
  const raw = config && typeof config === 'object' ? config : {}
  const projectDir = raw.projectDir ? String(raw.projectDir) : DEFAULT_PROJECT_DIR
  const tokenPath = raw.tokenPath ? String(raw.tokenPath) : join(projectDir, DEFAULT_TOKEN_FILE)
  return {
    projectDir,
    tokenPath,
    gatewayBaseUrl: raw.gatewayBaseUrl ? String(raw.gatewayBaseUrl) : `http://127.0.0.1:${DEFAULT_GATEWAY_PORT}`,
    mcpPort: DEFAULT_MCP_PORT,
  }
}

/** 读取本地 bearer token；缺失时给出可操作错误。 */
function readToken(settings) {
  try {
    const token = readFileSync(settings.tokenPath, 'utf8').trim()
    if (!token) throw new Error('token 文件为空')
    return token
  } catch (error) {
    const failure = new Error(`无法读取 ${settings.tokenPath}：${error.message}。请先在项目里运行 \`uv run python create_token.py\`。`)
    failure.code = 'token-missing'
    throw failure
  }
}

/** 向本地 gateway 发一次带 bearer 的请求。 */
async function gatewayRequest(settings, method, path, body) {
  const token = readToken(settings)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), GATEWAY_TIMEOUT_MS)
  try {
    const response = await fetch(new URL(path, settings.gatewayBaseUrl), {
      method,
      headers: {
        authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    const text = await response.text()
    let data = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        data = { raw: text.slice(0, 2000) }
      }
    }
    if (!response.ok) {
      const detail = data && typeof data === 'object' ? data.detail ?? data.error : undefined
      const failure = new Error(`gateway 返回 HTTP ${response.status}${detail ? `：${detail}` : ''}`)
      failure.code = 'gateway-error'
      throw failure
    }
    return data
  } catch (error) {
    if (error.name === 'AbortError') {
      const failure = new Error(`连接 gateway 超时（${settings.gatewayBaseUrl}）。请确认 gateway_host.py 正在运行。`)
      failure.code = 'gateway-offline'
      throw failure
    }
    if (error.code) throw error
    const failure = new Error(`无法连接 gateway（${settings.gatewayBaseUrl}）：${error.message}`)
    failure.code = 'gateway-offline'
    throw failure
  } finally {
    clearTimeout(timer)
  }
}

/** 探测 gateway 状态；失败不抛错，返回 reachable=false。 */
async function probeGateway(settings) {
  try {
    const [health, connections] = await Promise.all([
      gatewayRequest(settings, 'GET', '/health'),
      gatewayRequest(settings, 'GET', '/connections'),
    ])
    // /targets 是服务器端的最新配置；拿不到就退化为读本地 YAML。
    let targets = null
    try {
      const response = await gatewayRequest(settings, 'GET', '/targets')
      targets = response && typeof response.targets === 'object' ? response.targets : null
    } catch {
      targets = null
    }
    return { reachable: true, health, connections: connections?.connections ?? [], targets, error: null }
  } catch (error) {
    return { reachable: false, health: null, connections: [], targets: null, error: error.message }
  }
}

/**
 * catalog 方法：资产目录 + 连接状态。
 *
 * 目录来源优先级：gateway `/targets`（服务器端真实配置）> 本地 `config/log_targets.yaml`。
 * 本地 YAML 始终参与，用来补 env / kind / k8s 这些只影响面板展示的可选字段。
 */
async function loadCatalog(settings) {
  const loaded = loadTargets(settings.projectDir)
  const gateway = await probeGateway(settings)
  const localTargets = (loaded.raw && loaded.raw.targets) || {}
  const remoteTargets = gateway.targets && typeof gateway.targets === 'object' ? gateway.targets : null
  const entries = []
  if (remoteTargets && Object.keys(remoteTargets).length > 0) {
    for (const [key, target] of Object.entries(remoteTargets)) {
      entries.push({
        key,
        description: target?.description ?? '',
        modules: target?.modules ?? {},
        connection: target?.connection ?? null,
        optional: localTargets[key] ?? {},
      })
    }
    for (const [key, target] of Object.entries(localTargets)) {
      if (Object.prototype.hasOwnProperty.call(remoteTargets, key)) continue
      entries.push({
        key,
        description: target?.description ?? '',
        modules: target?.modules ?? {},
        connection: null,
        optional: target,
      })
    }
  } else {
    for (const [key, target] of Object.entries(localTargets)) {
      entries.push({
        key,
        description: target?.description ?? '',
        modules: target?.modules ?? {},
        connection: null,
        optional: target,
      })
    }
  }
  const { assets, environments } = buildCatalog(entries)
  return {
    generatedAt: new Date().toISOString(),
    projectDir: settings.projectDir,
    configFile: loaded.configFile,
    tokenFile: settings.tokenPath,
    catalogSource: remoteTargets ? 'gateway' : 'local-yaml',
    timezone: String(loaded.raw.log_timezone ?? 'Asia/Shanghai'),
    gateway: {
      baseUrl: settings.gatewayBaseUrl,
      reachable: gateway.reachable,
      connected: gateway.health?.assets_connected ?? 0,
      total: gateway.health?.assets_total ?? assets.length,
      error: gateway.error,
    },
    environments,
    assets,
  }
}

/** 从 kubectl 输出里取出资源名（`namespace/foo` 或纯名字）。 */
function parseKubectlNames(stdout) {
  return String(stdout ?? '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.split('/').pop().trim())
    .filter(Boolean)
}

/** 在一个资产上执行只读系统命令并返回 stdout。 */
async function systemQuery(settings, server, command) {
  const response = await gatewayRequest(settings, 'POST', '/execute-system-query', {
    servers: [server],
    command,
  })
  const results = Array.isArray(response?.results) ? response.results : []
  const result = results.find((item) => item && item.server === server) ?? results[0]
  if (!result) {
    const failure = new Error('gateway 未返回该资产的执行结果')
    failure.code = 'gateway-error'
    throw failure
  }
  if (result.error) {
    const failure = new Error(String(result.error))
    failure.code = 'query-rejected'
    throw failure
  }
  return String(result.stdout ?? '')
}

// ─────────────────────────── MCP 连接状态与手动重连 ───────────────────────────
// MCP 行由本 bundle 的 patch 插入；启动时网络未就绪会把重连预算烧完、工具被移除。
// 这里给面板和模型各留一条「手动重连」通路：重启那条 Loader 行（fiber.restart，不写盘）。

/** patch 行 id 与 MCP server 名。 */
const MCP_ROW_ID = 'log-context-mcp'
const MCP_SERVER_NAME = 'logmcp'

/** 安全读取服务，插件可选依赖缺失时返回 undefined。 */
function safeService(ctx, name) {
  try {
    return ctx.get(name)
  } catch {
    return undefined
  }
}

/** 当前已注册的 `mcp__logmcp__*` 工具名；拿不到 tools 服务时返回 null。 */
function mcpToolNames(ctx) {
  const tools = safeService(ctx, 'tools')
  if (!tools || typeof tools.definitions !== 'function') return null
  try {
    const prefix = `mcp__${MCP_SERVER_NAME}__`
    return tools
      .definitions()
      .map((definition) => (definition && typeof definition.name === 'string' ? definition.name : ''))
      .filter((name) => name.startsWith(prefix))
  } catch {
    return null
  }
}

/** MCP 连接摘要，供面板展示与工具返回。 */
function mcpStatus(ctx) {
  const names = mcpToolNames(ctx)
  return {
    serverName: MCP_SERVER_NAME,
    rowId: MCP_ROW_ID,
    known: names !== null,
    ready: names !== null && names.length > 0,
    toolCount: names ? names.length : 0,
    tools: names ?? [],
  }
}

/** 按 patch id 在 Loader 里找到那条行（entry id 可能带 `include:` 前缀）。 */
function findLoaderEntry(ctx, patchId) {
  const loader = safeService(ctx, 'loader')
  if (!loader || typeof loader.entries !== 'function') return undefined
  try {
    for (const entry of loader.entries()) {
      const id = String(entry?.id ?? '')
      const optionsId = String(entry?.options?.id ?? '')
      if (id === patchId || optionsId === patchId || id.endsWith(`:${patchId}`)) return entry
    }
  } catch {
    return undefined
  }
  return undefined
}

/** 等待 ms 毫秒。 */
function delay(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms))
}

/**
 * 重启 MCP 行：dispose 后立即重新 apply（不写 profile 配置）。
 * @param ctx - Host 插件上下文。
 * @param timeoutMs - 等待「重启完成」的上限；超时表示连接仍在进行。
 * @returns 重启前后工具数量与结论。
 */
async function restartMcpConnection(ctx, timeoutMs = 12000) {
  const before = mcpToolNames(ctx) || []
  const entry = findLoaderEntry(ctx, MCP_ROW_ID)
  if (!entry) {
    const failure = new Error(`找不到 Loader 行 ${MCP_ROW_ID}；请确认 bundle 已安装。`)
    failure.code = 'mcp-row-missing'
    throw failure
  }
  const fiber = entry.fiber
  if (!fiber || typeof fiber.restart !== 'function') {
    const failure = new Error(`Loader 行 ${entry.id} 当前未运行（被禁用？），无法重连。`)
    failure.code = 'mcp-row-inactive'
    throw failure
  }
  let outcome = 'done'
  try {
    outcome = await Promise.race([
      fiber.restart().then(() => 'done'),
      delay(timeoutMs).then(() => 'pending'),
    ])
  } catch (error) {
    outcome = `error: ${error?.message ?? String(error)}`
  }
  // 工具注册发生在 apply 之后，留一点时间再统计。
  await delay(300)
  const after = mcpToolNames(ctx) || []
  return {
    outcome,
    readyBefore: before.length > 0,
    readyAfter: after.length > 0,
    toolCount: after.length,
    tools: after,
  }
}

/** 模型可调用的手动重连工具名。 */
const MCP_RESTART_TOOL_NAME = 'log_context_mcp_restart'

/**
 * 构造「重连 MCP」工具：MCP 工具被移除后模型仍可用它把工具救回来。
 * @param ctx - Host 插件上下文。
 * @returns 一份符合 ToolDefinition 形状的普通对象。
 */
function createRestartTool(ctx) {
  return {
    name: MCP_RESTART_TOOL_NAME,
    description:
      '重启 log-context 插件的 MCP 连接。仅在 `mcp__logmcp__*` 工具不可用时使用（例如 DSH 启动时网络/VPN 未就绪，MCP 客户端重连预算耗尽后工具被移除）；正常情况下不需要调用。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render(_args, value) {
        return [{ type: 'text', text: String(value ?? '') }]
      },
    },
    async execute() {
      try {
        const result = await restartMcpConnection(ctx)
        if (result.readyAfter) {
          return `已重启 MCP 连接，${result.toolCount} 个 mcp__logmcp__* 工具恢复可用（重启前 ${result.readyBefore ? '可用' : '不可用'}）。`
        }
        return `已触发重连，但工具仍未就绪（结果：${result.outcome}）。请确认 10.12.0.101:8000 可达、.token 正确，然后再次调用本工具或刷新日志上下文面板。`
      } catch (error) {
        return `重连 MCP 失败：${error?.message ?? String(error)}`
      }
    },
  }
}

/** API 方法分发表。 */
function createDispatcher(ctx, settings) {
  return {
    async catalog() {
      const catalog = await loadCatalog(settings)
      catalog.prompt = buildPromptHints(catalog)
      catalog.mcp = mcpStatus(ctx)
      return catalog
    },
    async 'mcp.status'() {
      return mcpStatus(ctx)
    },
    async 'mcp.restart'() {
      return restartMcpConnection(ctx)
    },
    async connect(payload) {
      const server = String(payload?.server ?? '').trim()
      if (!server) {
        const failure = new Error('缺少 server 参数')
        failure.code = 'bad-request'
        throw failure
      }
      const result = await gatewayRequest(settings, 'POST', '/connect', { server })
      return { server, result }
    },
    async 'k8s.namespaces'(payload) {
      const server = String(payload?.server ?? '').trim()
      if (!server) {
        const failure = new Error('缺少 server 参数')
        failure.code = 'bad-request'
        throw failure
      }
      const stdout = await systemQuery(settings, server, 'kubectl get namespaces -o name')
      return { server, namespaces: parseKubectlNames(stdout) }
    },
    async 'k8s.pods'(payload) {
      const server = String(payload?.server ?? '').trim()
      const namespace = String(payload?.namespace ?? '').trim()
      if (!server || !namespace) {
        const failure = new Error('缺少 server 或 namespace 参数')
        failure.code = 'bad-request'
        throw failure
      }
      const safeNamespace = namespace.replace(/[^A-Za-z0-9._-]/g, '')
      const stdout = await systemQuery(settings, server, `kubectl get pods -n ${safeNamespace} -o name`)
      return { server, namespace: safeNamespace, pods: parseKubectlNames(stdout) }
    },
  }
}

/** 给面板的提示：模型侧可用工具名随 serverName 变化，这里只描述用途。 */
function buildPromptHints(catalog) {
  const connected = catalog.assets.filter((asset) => asset.connection && asset.connection.state === 'connected').length
  return {
    serverName: 'logmcp',
    connected,
    total: catalog.assets.length,
  }
}

// ─────────────────────────── HTTP 路由 ───────────────────────────

function sendJson(res, status, value) {
  const body = JSON.stringify(value)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  res.end(body)
}

function readJsonBody(req) {
  return new Promise((resolvePromise, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error('请求体过大'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8')
      if (!text.trim()) {
        resolvePromise({})
        return
      }
      try {
        resolvePromise(JSON.parse(text))
      } catch (error) {
        reject(new Error(`请求体不是合法 JSON：${error.message}`))
      }
    })
    req.on('error', reject)
  })
}

const SYSTEM_PROMPT_SECTION = [
  '## 日志与运维上下文（log-context 插件）',
  '本会话通过 MCP 接入 JumpServer 日志网关，工具名前缀为 `mcp__logmcp__`。',
  '当用户消息中出现「【日志上下文】」块时，块里已经给出这次要看的资产、模块/日志目录或 K8S 命名空间/Pod，按块里的信息直接查询，不要反问「查哪个服务器 / 哪个模块」。',
  '- 连接状态：先看 `mcp__logmcp__list_connections`；资产未连接时用 `mcp__logmcp__reconnect_asset` 触发重连（MFA 在 gateway 终端完成）。',
  '- Linux 日志：`mcp__logmcp__search_logs`（固定字符串搜索）、`mcp__logmcp__read_log_tail`（读末尾）、`mcp__logmcp__execute_query`（受限只读命令，如 grep/zgrep/awk）。',
  '- K8S：资产类型为 K8S 时用 `mcp__logmcp__execute_system_query` 执行只读 kubectl，例如 `kubectl get pods -n <ns>`、`kubectl logs -n <ns> <pod> --tail=200`、`kubectl describe pod -n <ns> <pod>`、`kubectl get events -n <ns>`。',
  '- 只有块里确实缺少必要信息（例如没给时间范围、也没给要搜的关键字）时，才问一个最小的澄清问题。',
].join('\n')

// ─────────────────────────── 插件入口 ───────────────────────────

/**
 * 注册路由、系统提示；参数来自 bundle 行的 `config`。
 * @param ctx - Host 插件上下文。
 * @param config - `{ projectDir?, tokenPath?, gatewayBaseUrl? }`。
 */
export function apply(ctx, config) {
  const settings = resolveSettings(config)
  const dispatch = createDispatcher(ctx, settings)

  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: API_PATH,
      handler: async (req, res) => {
        const rejection = ctx.connection.requestRejection({ headers: req.headers })
        if (rejection !== undefined) {
          const message =
            rejection === 401
              ? '未通过浏览器鉴权：请用打开 DSH 页面的同一浏览器会话访问（刷新页面后重试）。'
              : '请求被拒绝：只接受同源、本机浏览器的请求。'
          sendJson(res, rejection, { ok: false, error: { code: 'forbidden', message } })
          return
        }
        if (req.method !== 'POST') {
          sendJson(res, 405, { ok: false, error: { code: 'method-not-allowed', message: '仅支持 POST' } })
          return
        }
        let payload
        try {
          payload = await readJsonBody(req)
        } catch (error) {
          sendJson(res, 400, { ok: false, error: { code: 'bad-request', message: error.message } })
          return
        }
        const method = String(payload?.method ?? '')
        const handler = Object.prototype.hasOwnProperty.call(dispatch, method) ? dispatch[method] : undefined
        if (typeof handler !== 'function') {
          sendJson(res, 404, { ok: false, error: { code: 'unknown-method', message: `未知方法：${method}` } })
          return
        }
        try {
          const value = await handler.call(dispatch, payload?.payload ?? {})
          sendJson(res, 200, { ok: true, value })
        } catch (error) {
          sendJson(res, 200, {
            ok: false,
            error: { code: error?.code ?? 'internal', message: error?.message ?? String(error) },
          })
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.systemPrompt.section({
      name: 'log-context',
      order: 60,
      text: SYSTEM_PROMPT_SECTION,
    }),
  )

  // 手动重连工具：MCP 工具被移除后，模型仍有一条自救通路。
  ctx.effect(() => {
    const tools = safeService(ctx, 'tools')
    if (!tools || typeof tools.register !== 'function') return undefined
    try {
      return tools.register(createRestartTool(ctx))
    } catch (error) {
      console.warn('[log-context] 注册重连工具失败:', error)
      return undefined
    }
  })
}

// 导出内部工具函数，便于在 Node 里直接验证解析/派生/网关逻辑（不影响插件运行）。
export const internals = {
  parseYaml,
  buildCatalog,
  inferEnv,
  inferKind,
  loadCatalog,
  probeGateway,
  gatewayRequest,
  systemQuery,
  parseKubectlNames,
  resolveSettings,
  mcpStatus,
  findLoaderEntry,
  restartMcpConnection,
  createRestartTool,
  MCP_ROW_ID,
  MCP_RESTART_TOOL_NAME,
}
