/**
 * 联调自检（真实环境，只读）：直接访问远程 gateway，验证 catalog 与 K8S 命名空间拉取。
 *
 *   cd <project>
 *   node dsh-plugins/log-context/check-live.mjs
 *
 * 只调用 gateway 的只读接口（/health、/connections、/targets、/execute-system-query 的
 * kubectl get 命令），不触发任何认证或写操作。参数与 cordis.patch.yml 保持一致。
 */
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { internals } from './index.js'

const here = dirname(fileURLToPath(import.meta.url))
const settings = {
  projectDir: join(here, '..', '..'),
  tokenPath: join(here, '.token'),
  gatewayBaseUrl: process.env.LOG_CONTEXT_GATEWAY ?? 'http://10.12.0.101:8765',
}

console.log('目标 gateway:', settings.gatewayBaseUrl)
console.log('token 文件:', settings.tokenPath)

const catalog = await internals.loadCatalog(settings)
console.log(
  `gateway 可达: ${catalog.gateway.reachable} | 已连接 ${catalog.gateway.connected}/${catalog.gateway.total} | 目录来源 ${catalog.catalogSource}`,
)
if (!catalog.gateway.reachable) throw new Error(catalog.gateway.error || 'gateway 不可达')

console.log(`资产 ${catalog.assets.length} 个，环境: ${catalog.environments.map((item) => `${item.label}(${item.count})`).join(' ')}`)
for (const asset of catalog.assets) {
  const state = asset.connection ? asset.connection.state : '未知'
  console.log(`  [${asset.kind}] ${asset.key} | ${state} | 模块 ${asset.modules.length}`)
}

const k8s = catalog.assets.filter((asset) => asset.kind === 'k8s' && asset.connection && asset.connection.state === 'connected')
if (k8s.length === 0) {
  console.log('没有已连接的 K8S 资产，跳过命名空间拉取')
} else {
  const target = k8s[0]
  console.log(`\n向 ${target.key} 拉取命名空间…`)
  const stdout = await internals.systemQuery(settings, target.key, 'kubectl get namespaces -o name')
  const namespaces = internals.parseKubectlNames(stdout)
  console.log('命名空间:', namespaces.length ? namespaces.join(', ') : '(空)')
}

console.log('\n联调检查通过')
