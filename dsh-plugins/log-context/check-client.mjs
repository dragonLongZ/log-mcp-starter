/**
 * 开发期自检（不需要浏览器）：验证 client.js 的语法、工厂执行、槽/标签注册参数与提示词生成。
 *
 *   cd <project>
 *   node dsh-plugins/log-context/check-client.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))

let captured = null
globalThis.window = {
  __ModuleLoader__: {
    load(entry) {
      captured = entry
    },
  },
  localStorage: {
    _data: new Map(),
    getItem(key) {
      return this._data.has(key) ? this._data.get(key) : null
    },
    setItem(key, value) {
      this._data.set(key, String(value))
    },
  },
  setTimeout: (fn) => setTimeout(fn, 0),
}

const ReactStub = {
  createElement(type, props, ...children) {
    return { __el: true, type, props: props || {}, children }
  },
  useState(initial) {
    return [typeof initial === 'function' ? initial() : initial, () => {}]
  },
  useEffect() {},
  useMemo(factory) {
    return factory()
  },
  useCallback(fn) {
    return fn
  },
  useRef(initial) {
    return { current: initial }
  },
}

const requireStub = (name) => {
  if (name === 'react') return ReactStub
  throw new Error(`unexpected require: ${name}`)
}

const source = readFileSync(join(here, 'client.js'), 'utf8')
const script = new Function('window', `${source}\n//# sourceURL=log-context-client.js`)
script(globalThis.window)

if (!captured) throw new Error('client.js 未调用 __ModuleLoader__.load')
if (captured.id !== '@zxlll/dsh-log-context') throw new Error(`意外的模块 id: ${captured.id}`)
console.log('OK 模块 id:', captured.id)

const plugin = captured.factory(requireStub)
if (!plugin || !Array.isArray(plugin.inject)) throw new Error('缺少 inject 声明')
if (typeof plugin.apply !== 'function') throw new Error('缺少 apply')
console.log('OK inject:', plugin.inject.join(','))

// ── 注册路径：伪造 ctx，验证 sidebarRightTabs.register / slots.register 的参数形状 ──
const registrations = []
const fakeCtx = {
  slots: {
    inject(_key, callback) {
      const disposer = callback()
      return disposer || (() => {})
    },
    register(options, Component) {
      registrations.push({ kind: 'slot', key: options.name, options, Component })
      return () => {}
    },
  },
  inject(_deps, callback) {
    const services = {
      sidebarRightTabs: {
        register(definition) {
          registrations.push({ kind: 'tab-type', definition })
          return () => {}
        },
      },
      sidebarRight: { openTab() {} },
    }
    const disposable = callback({ get: (name) => services[name] })
    return {
      dispose: () => {
        if (typeof disposable === 'function') disposable()
      },
    }
  },
}
plugin.apply(fakeCtx)

const tabType = registrations.find((item) => item.kind === 'tab-type')
if (!tabType) throw new Error('未注册右栏标签类型')
console.log(
  'OK 标签类型:',
  JSON.stringify({ id: tabType.definition.id, kind: tabType.definition.kind, guide: tabType.definition.guide.length }),
)
const slots = registrations.filter((item) => item.kind === 'slot')
console.log('OK 槽注册:', slots.map((item) => `${item.key}:${item.options.key ?? item.options.id}`).join(' , '))

// ── 渲染冒烟：假 hooks 跑一遍组件函数，抓 ReferenceError ──
const tabSlot = slots.find((item) => item.key === 'sidebar.right.pane.tab')
if (!tabSlot) throw new Error('未注册 sidebar.right.pane.tab')
const tree = tabSlot.Component({
  inputActions: { captureInsertion: () => ({}), insertText: () => true, setDraft: () => {}, submit: () => {} },
  useInput: (selector) => selector({ draft: '' }),
  useTabInfo: () => ({ tab: { visible: true } }),
})
if (!JSON.stringify(tree).includes('日志上下文')) throw new Error('渲染结果中缺少标题')
console.log('OK 渲染冒烟：节点树长度', JSON.stringify(tree).length)

const buttonSlot = slots.find((item) => item.key === 'conversation.input.left')
if (!buttonSlot) throw new Error('未注册 conversation.input.left')
if (!JSON.stringify(buttonSlot.Component({})).includes('日志上下文')) throw new Error('入口按钮渲染失败')
console.log('OK 输入框入口按钮渲染')

// ── 提示词生成 ──
const internals = plugin.internals
if (!internals) throw new Error('缺少 internals 测试出口')
const linuxPrompt = internals.buildPrompt({
  asset: {
    key: 'server-172.26.73.42-2.0-prod',
    description: '2.0云平台生产环境jumpserver资产的ip尾号为11的服务器',
    kind: 'linux',
    modules: [{ name: 'member', description: '会员服务', logDir: '/data/logs/parking/member' }],
  },
  moduleName: 'member',
  template: internals.LINUX_TEMPLATES[0],
  keyword: '',
  day: 'today',
  customDay: '',
})
const k8sPrompt = internals.buildPrompt({
  asset: { key: 'server-172.26.73.83-2.0-dev', description: '2.0平台研发环境 K8s 资产节点', kind: 'k8s', modules: [] },
  moduleName: '',
  template: internals.K8S_TEMPLATES[0],
  keyword: '',
  day: 'today',
  customDay: '',
  namespace: 'parking-prod',
  pod: 'cyf-cloud-parking-7d9f-abc',
})
console.log('--- Linux 提示词 ---')
console.log(linuxPrompt)
console.log('--- K8S 提示词 ---')
console.log(k8sPrompt)

if (!linuxPrompt.includes('/data/logs/parking/member')) throw new Error('Linux 提示词缺少日志目录')
if (!k8sPrompt.includes('parking-prod') || !k8sPrompt.includes('cyf-cloud-parking-7d9f-abc')) {
  throw new Error('K8S 提示词缺少命名空间/Pod')
}
console.log('全部检查通过')
