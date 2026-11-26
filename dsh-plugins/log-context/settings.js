/**
 * 插件连接设置的持久化（共享给 Host 半与 bundle patch 里的 `!!js` 表达式）。
 *
 * 为什么放在 DSH home 而不是仓库目录：
 *   1. 设置里有 token（凭据），不能进 git；
 *   2. 别人 clone 仓库后不需要改任何文件就能配置自己的网关；
 *   3. patch 里的 `!!js` 表达式拿不到「当前插件目录」，只能算出一个确定的绝对路径。
 *
 * 文件位置：`$DSH_HOME/log-context/settings.json`，未设 DSH_HOME 时为 `~/.dsh/log-context/settings.json`。
 *
 * 字段：
 *   gatewayBaseUrl  网关 HTTP 地址（资产目录 / 连接状态 / kubectl 只读查询）
 *   mcpUrl          MCP 端点（模型侧 mcp__logmcp__* 工具）
 *   token           bearer token（网关与 MCP 共用同一把）
 *   updatedAt       最后保存时间（仅展示用）
 */
import { readFileSync, writeFileSync, mkdirSync, renameSync, unlinkSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { homedir } from 'node:os'

/** 设置目录名（位于 DSH home 下）。 */
export const SETTINGS_DIR_NAME = 'log-context'
/** 设置文件名。 */
export const SETTINGS_FILE_NAME = 'settings.json'

/** 解析 DSH home：显式配置 > $DSH_HOME > ~/.dsh（与 dsh-home-paths 的优先级一致）。 */
export function resolveDshHome(env = process.env, home = homedir()) {
  const configured = String(env?.DSH_HOME ?? '').trim()
  if (configured) return configured
  return join(home, '.dsh')
}

/** 设置文件的绝对路径。 */
export function settingsPath(env = process.env, home = homedir()) {
  return join(resolveDshHome(env, home), SETTINGS_DIR_NAME, SETTINGS_FILE_NAME)
}

/** 读取设置；文件不存在或损坏时返回 null（调用方用默认值兜底）。 */
export function readSettings(file = settingsPath()) {
  try {
    const raw = readFileSync(file, 'utf8')
    // 去掉 UTF-8 BOM：Windows 记事本 / PowerShell 写的 JSON 常带 BOM，
    // JSON.parse 会直接抛 "Unexpected token '﻿'"，导致设置被静默忽略。
    const parsed = JSON.parse(raw.replace(/^\uFEFF/, ''))
    if (!parsed || typeof parsed !== 'object') return null
    return {
      gatewayBaseUrl: typeof parsed.gatewayBaseUrl === 'string' ? parsed.gatewayBaseUrl : '',
      mcpUrl: typeof parsed.mcpUrl === 'string' ? parsed.mcpUrl : '',
      token: typeof parsed.token === 'string' ? parsed.token : '',
      updatedAt: typeof parsed.updatedAt === 'string' ? parsed.updatedAt : '',
    }
  } catch {
    return null
  }
}

/** 原子写入设置：先写临时文件再 rename，避免读到半个 JSON。 */
export function writeSettings(next, file = settingsPath()) {
  const payload = {
    gatewayBaseUrl: String(next.gatewayBaseUrl ?? '').trim(),
    mcpUrl: String(next.mcpUrl ?? '').trim(),
    token: String(next.token ?? '').trim(),
    updatedAt: new Date().toISOString(),
  }
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.tmp`
  try {
    writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
    renameSync(tmp, file)
  } catch (error) {
    try {
      unlinkSync(tmp)
    } catch {
      /* 临时文件可能没建成 */
    }
    throw error
  }
  return payload
}

/** 只保留首尾字符，用于界面展示（避免把完整 token 打到页面上）。 */
export function maskToken(token) {
  const value = String(token ?? '')
  if (!value) return ''
  if (value.length <= 8) return `${value.slice(0, 1)}***`
  return `${value.slice(0, 4)}***${value.slice(-4)}`
}

/** 校验并规范化 URL：必须以 http(s):// 开头。 */
export function normalizeUrl(raw, label) {
  const value = String(raw ?? '').trim()
  if (!value) throw new Error(`${label}不能为空`)
  if (!/^https?:\/\//i.test(value)) throw new Error(`${label}必须以 http:// 或 https:// 开头`)
  try {
    new URL(value)
  } catch {
    throw new Error(`${label}不是合法的 URL：${value}`)
  }
  return value.replace(/\/+$/, '')
}

/**
 * 合并出运行期设置。
 * 优先级：设置文件（用户在右栏保存过）> bundle patch 的 config > 默认值。
 *
 * @param config - bundle patch 行 config：`{ projectDir?, tokenPath?, gatewayBaseUrl?, mcpUrl? }`
 * @param overrides - 测试用：`{ file, env, home }`
 */
export function resolveSettings(config, overrides = {}) {
  const raw = config && typeof config === 'object' ? config : {}
  const file = overrides.file ?? settingsPath(overrides.env, overrides.home)
  const stored = readSettings(file) || {}
  const projectDir = raw.projectDir ? String(raw.projectDir) : ''
  const fallbackTokenPath = raw.tokenPath
    ? String(raw.tokenPath)
    : projectDir
      ? join(projectDir, 'local.token')
      : ''
  return {
    projectDir,
    settingsFile: file,
    tokenPath: fallbackTokenPath,
    gatewayBaseUrl: stored.gatewayBaseUrl || String(raw.gatewayBaseUrl || 'http://127.0.0.1:8765'),
    mcpUrl: stored.mcpUrl || String(raw.mcpUrl || 'http://127.0.0.1:8000/mcp'),
    token: stored.token || '',
    source: {
      gatewayBaseUrl: stored.gatewayBaseUrl ? 'settings' : 'patch',
      mcpUrl: stored.mcpUrl ? 'settings' : 'patch',
      token: stored.token ? 'settings' : 'file',
    },
  }
}
