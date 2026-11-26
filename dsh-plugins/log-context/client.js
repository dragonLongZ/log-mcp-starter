/**
 * Client 半：Web GUI 右栏「日志上下文」标签页。
 *
 * 做的事只有一件：把「资产 / 模块 / K8S 命名空间 / 提示词模板」变成一段结构化提示词，
 * 一键写进当前会话的输入框，让模型不必再反问"查哪台机器、哪个模块"。
 *
 * 数据来自 Host 半的 POST /log-context/api；MCP 工具由 bundle 的另一个 patch 行接入。
 */
window.__ModuleLoader__.load({
  // 必须与 package.json 的 name 一致：client 模块系统按包名登记/查找模块。
  id: '@zxlll/dsh-log-context',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** 右栏标签类型：openTab 用它打开页面。 */
    const KIND = 'log-context'
    /** 标签实现 id，也是 sidebar.right.pane.tab 槽的 key，必须全局唯一。 */
    const IMPL_ID = '@zxlll/dsh-log-context'
    const API_URL = '/log-context/api'
    const STORAGE_KEY = 'dsh.log-context.v1'

    const DAY_OPTIONS = [
      { id: 'today', label: '今天' },
      { id: 'yesterday', label: '昨天' },
      { id: 'custom', label: '指定日期' },
    ]

    /** 普通 Linux 资产的动作模板。 */
    const LINUX_TEMPLATES = [
      {
        id: 'error-today',
        label: '查今天 error',
        day: 'today',
        logType: 'error',
        action: '搜索今天 error 日志中的异常，定位最早出现的错误和它相关的调用链',
      },
      {
        id: 'tail',
        label: '最近 200 行',
        day: 'today',
        logType: 'both',
        action: '读取最近 200 行日志，先概览当前运行状态与报错',
      },
      {
        id: 'keyword',
        label: '搜关键字',
        day: 'today',
        logType: 'both',
        keyword: true,
        action: '搜索关键字「{keyword}」，按时间顺序列出命中行并给出上下文',
      },
      {
        id: 'system',
        label: '系统与容器',
        action: '查看系统负载、磁盘、内存与容器状态（uptime / df -h / free -m / docker ps）',
      },
      { id: 'browse', label: '只给上下文', action: '' },
    ]

    /** K8S 资产的动作模板。 */
    const K8S_TEMPLATES = [
      { id: 'k8s-logs', label: 'Pod 日志', action: '读取该 Pod 最近 200 行日志（kubectl logs --tail=200）' },
      { id: 'k8s-events', label: 'Pod 事件', action: '查看该命名空间的事件与 Pod 事件（kubectl get events / describe pod）' },
      { id: 'k8s-status', label: 'Pod 状态', action: '查看该命名空间 Pod 列表与运行状态（kubectl get pods -o wide）' },
      { id: 'k8s-workload', label: '工作负载', action: '查看 Deployment / StatefulSet 的期望副本与实际副本' },
      { id: 'k8s-browse', label: '只给上下文', action: '' },
    ]

    // ───────────────────────── 数据访问 ─────────────────────────

    /** 调用 Host 半的接口；失败时抛出可直接展示的错误。 */
    async function callApi(method, payload, signal) {
      let response
      try {
        response = await fetch(API_URL, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ method, payload: payload ?? {} }),
          signal,
        })
      } catch (error) {
        if (error && error.name === 'AbortError') throw error
        throw new Error(`无法连接插件 Host 半：${error && error.message ? error.message : String(error)}`)
      }
      let envelope
      try {
        envelope = await response.json()
      } catch {
        throw new Error(`Host 返回了非 JSON 响应（HTTP ${response.status}）`)
      }
      if (!envelope || envelope.ok !== true) {
        const message = envelope && envelope.error && envelope.error.message ? envelope.error.message : `HTTP ${response.status}`
        throw new Error(message)
      }
      return envelope.value
    }

    /** 读取上次选择，避免每次重开都从头点。 */
    function loadPrefs() {
      try {
        const raw = window.localStorage.getItem(STORAGE_KEY)
        if (!raw) return {}
        const parsed = JSON.parse(raw)
        return parsed && typeof parsed === 'object' ? parsed : {}
      } catch {
        return {}
      }
    }

    /** 记住这次选择。 */
    function savePrefs(next) {
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      } catch {
        /* 隐私模式等场景下忽略 */
      }
    }

    /** 连接状态 → 展示色与文案。 */
    function connectionOf(asset) {
      const state = asset && asset.connection ? asset.connection.state : 'unknown'
      if (state === 'connected') return { color: 'var(--dsw-alias-state-success-primary)', label: '已连接' }
      if (state === 'connecting') return { color: 'var(--dsw-alias-state-warn-primary)', label: '认证中' }
      if (state === 'needs_reauth') return { color: 'var(--dsw-alias-state-warn-primary)', label: '需重新认证' }
      if (state === 'disconnected') return { color: 'var(--dsw-alias-state-error-primary)', label: '未连接' }
      return { color: 'var(--dsw-alias-state-idle-primary)', label: '状态未知' }
    }

    /** 生成要插入输入框的提示词块。 */
    function buildPrompt(ctx) {
      const { asset, moduleName, template, keyword, day, customDay, namespace, pod } = ctx
      const lines = ['【日志上下文】']
      if (!asset) {
        lines.push('资产：（未选择，请先在右栏「日志上下文」里选择资产与模块）')
        lines.push('问题：')
        return lines.join('\n') + '\n'
      }
      const description = asset.description ? `（${asset.description}）` : ''
      lines.push(`资产：${asset.key}${description}`)
      if (asset.kind === 'k8s') {
        lines.push('类型：K8S 集群（用 execute_system_query 执行只读 kubectl）')
        if (namespace) lines.push(`命名空间：${namespace}`)
        if (pod) lines.push(`Pod：${pod}`)
      } else {
        const moduleConfig = (asset.modules || []).find((item) => item.name === moduleName)
        const moduleDescription = moduleConfig && moduleConfig.description ? `（${moduleConfig.description}）` : ''
        lines.push(`模块：${moduleName || '(未选择模块)'}${moduleDescription}`)
        if (moduleConfig && moduleConfig.logDir) lines.push(`日志目录：${moduleConfig.logDir}`)
      }
      const dayValue = day === 'custom' ? customDay || 'today' : day
      if (template && (template.day || template.logType)) {
        const dayText = dayValue === 'today' ? '今天（today）' : dayValue === 'yesterday' ? '昨天（yesterday）' : dayValue
        lines.push(`时间范围：${template.day ? dayText : '不限'}`)
        if (template.logType) lines.push(`日志类型：${template.logType === 'error' ? 'error 优先' : 'info + error'}`)
      }
      if (template && template.keyword) lines.push(`关键字：${keyword || '(待补充)'}`)
      if (template && template.action) {
        lines.push(`动作：${template.action.replace('{keyword}', keyword || '关键字')}`)
      }
      lines.push('问题：')
      return lines.join('\n') + '\n'
    }

    // ───────────────────────── 组件 ─────────────────────────

    const STYLE_TEXT = `
      .lc-root { display:flex; flex-direction:column; height:100%; min-height:0; color:var(--dsw-alias-label-primary); font-size:13px; }
      .lc-head { display:flex; align-items:center; gap:8px; padding:10px 12px 8px; border-bottom:1px solid var(--dsw-alias-border-l1); }
      .lc-head-title { font-size:13px; font-weight:600; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .lc-dot { width:7px; height:7px; border-radius:50%; flex:none; }
      .lc-icon-btn { display:inline-flex; align-items:center; justify-content:center; height:24px; min-width:24px; padding:0 6px; border-radius:6px; border:1px solid transparent; background:transparent; color:var(--dsw-alias-label-secondary); font-size:12px; cursor:pointer; }
      .lc-icon-btn:hover { background:var(--dsw-alias-bg-layer-2); color:var(--dsw-alias-label-primary); }
      .lc-icon-btn:disabled { opacity:.45; cursor:default; }
      .lc-body { flex:1; min-height:0; overflow:auto; padding:8px 12px 12px; display:flex; flex-direction:column; gap:10px; }
      .lc-group { display:flex; flex-direction:column; gap:6px; }
      .lc-label { font-size:11px; font-weight:600; letter-spacing:.04em; color:var(--dsw-alias-label-secondary); text-transform:uppercase; }
      .lc-input { width:100%; box-sizing:border-box; padding:6px 8px; border-radius:6px; border:1px solid var(--dsw-alias-border-l1); background:var(--dsw-alias-bg-layer-2); color:var(--dsw-alias-label-primary); font-size:12px; outline:none; }
      .lc-input:focus { border-color:var(--dsw-alias-brand-primary); }
      .lc-chips { display:flex; flex-wrap:wrap; gap:5px; }
      .lc-chip { padding:3px 9px; border-radius:999px; border:1px solid var(--dsw-alias-border-l1); background:transparent; color:var(--dsw-alias-label-secondary); font-size:12px; cursor:pointer; line-height:18px; }
      .lc-chip:hover { background:var(--dsw-alias-bg-layer-2); color:var(--dsw-alias-label-primary); }
      .lc-chip[data-active='true'] { border-color:var(--dsw-alias-brand-primary); color:var(--dsw-alias-brand-primary); background:var(--dsw-alias-bg-layer-2); }
      .lc-list { display:flex; flex-direction:column; gap:2px; max-height:190px; overflow:auto; border:1px solid var(--dsw-alias-border-l1); border-radius:8px; padding:4px; background:var(--dsw-alias-bg-layer-1); }
      .lc-row { display:flex; align-items:center; gap:8px; padding:6px 8px; border-radius:6px; border:none; background:transparent; text-align:left; width:100%; cursor:pointer; color:var(--dsw-alias-label-primary); font-size:12px; }
      .lc-row:hover { background:var(--dsw-alias-bg-layer-2); }
      .lc-row[data-active='true'] { background:var(--dsw-alias-bg-layer-2); box-shadow:inset 0 0 0 1px var(--dsw-alias-border-l2); }
      .lc-row-main { flex:1; min-width:0; display:flex; flex-direction:column; gap:1px; }
      .lc-row-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .lc-row-sub { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--dsw-alias-label-secondary); font-size:11px; }
      .lc-empty { padding:10px; text-align:center; color:var(--dsw-alias-label-secondary); font-size:12px; }
      .lc-readout { background:var(--dsw-alias-bg-layer-2); border:1px solid var(--dsw-alias-border-l1); border-radius:8px; padding:8px; }
      .lc-preview { width:100%; box-sizing:border-box; height:126px; resize:vertical; border:none; background:transparent; color:var(--dsw-alias-label-primary); font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:11.5px; line-height:1.5; outline:none; }
      .lc-actions { display:flex; gap:6px; }
      .lc-btn { flex:1; padding:7px 10px; border-radius:7px; border:1px solid var(--dsw-alias-border-l1); background:var(--dsw-alias-bg-layer-2); color:var(--dsw-alias-label-primary); font-size:12px; font-weight:500; cursor:pointer; }
      .lc-btn:hover { border-color:var(--dsw-alias-border-l2); }
      .lc-btn[data-primary='true'] { background:var(--dsw-alias-brand-primary); border-color:var(--dsw-alias-brand-primary); color:#fff; }
      .lc-btn:disabled { opacity:.5; cursor:default; }
      .lc-notice { font-size:11.5px; color:var(--dsw-alias-label-secondary); min-height:16px; }
      .lc-warn { border:1px solid var(--dsw-alias-state-warn-primary); border-radius:8px; padding:8px 10px; display:flex; align-items:center; gap:8px; font-size:11.5px; color:var(--dsw-alias-state-warn-primary); }
      .lc-warn-text { flex:1; line-height:1.45; }
      .lc-error { border:1px solid var(--dsw-alias-state-error-primary); border-radius:8px; padding:10px; font-size:12px; color:var(--dsw-alias-state-error-primary); display:flex; flex-direction:column; gap:8px; }
      .lc-open-btn { display:inline-flex; align-items:center; gap:5px; height:26px; padding:0 9px; border-radius:6px; border:1px solid var(--dsw-alias-border-l1); background:transparent; color:var(--dsw-alias-label-secondary); font-size:12px; cursor:pointer; }
      .lc-open-btn:hover { background:var(--dsw-alias-bg-layer-2); color:var(--dsw-alias-label-primary); }
      .lc-hint { font-size:11px; color:var(--dsw-alias-label-secondary); line-height:1.5; }
    `

    /** 右栏标签页主体。 */
    function LogContextTab(props) {
      const inputActions = props.inputActions
      const useInput = props.useInput
      const useTabInfo = props.useTabInfo

      // 框架提供的 tab 信息钩子；缺失时当作可见，保证不会因为拿不到它而崩。
      const tabInfo = typeof useTabInfo === 'function' ? useTabInfo() : null
      const visible = tabInfo && tabInfo.tab ? tabInfo.tab.visible !== false : true

      const draft = typeof useInput === 'function' ? useInput((state) => (state ? state.draft : '')) : ''

      const prefs = React.useRef(loadPrefs())
      const [catalog, setCatalog] = React.useState(null)
      const [loading, setLoading] = React.useState(true)
      const [error, setError] = React.useState(null)
      const [refreshTick, setRefreshTick] = React.useState(0)
      const [envFilter, setEnvFilter] = React.useState('all')
      const [query, setQuery] = React.useState('')
      const [assetKey, setAssetKey] = React.useState(prefs.current.assetKey || '')
      const [moduleName, setModuleName] = React.useState(prefs.current.moduleName || '')
      const [templateId, setTemplateId] = React.useState(prefs.current.templateId || '')
      const [keyword, setKeyword] = React.useState('')
      const [day, setDay] = React.useState(prefs.current.day || 'today')
      const [customDay, setCustomDay] = React.useState('')
      const [namespace, setNamespace] = React.useState(prefs.current.namespace || '')
      const [pod, setPod] = React.useState('')
      const [namespaceOptions, setNamespaceOptions] = React.useState([])
      const [podOptions, setPodOptions] = React.useState([])
      const [busy, setBusy] = React.useState('')
      const [notice, setNotice] = React.useState('')
      // 连接设置：默认折叠，避免挡住日常选择资产的流程。
      const [connOpen, setConnOpen] = React.useState(false)
      const [connForm, setConnForm] = React.useState({ gatewayBaseUrl: '', mcpUrl: '', token: '' })
      const [connTesting, setConnTesting] = React.useState('')
      const [connNotice, setConnNotice] = React.useState('')
      // MCP 端点探活结果：用来区分「地址/token 不对」与「只是客户端没注册工具」。
      const [mcpProbe, setMcpProbe] = React.useState(null)

      React.useEffect(() => {
        const controller = new AbortController()
        let alive = true
        setLoading(true)
        setError(null)
        callApi('catalog', {}, controller.signal)
          .then((value) => {
            if (!alive) return
            setCatalog(value)
            setLoading(false)
          })
          .catch((failure) => {
            if (!alive || (failure && failure.name === 'AbortError')) return
            setError(failure.message || String(failure))
            setLoading(false)
          })
        return () => {
          alive = false
          controller.abort()
        }
      }, [refreshTick])

      // 切到本标签（含从后台切回）时刷新一次连接状态。
      React.useEffect(() => {
        if (visible && catalog) setRefreshTick((tick) => tick + 1)
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [visible])

      const assets = (catalog && catalog.assets) || []
      const environments = (catalog && catalog.environments) || []
      const connection = (catalog && catalog.connection) || null
      const asset = assets.find((item) => item.key === assetKey) || null
      const templates = asset && asset.kind === 'k8s' ? K8S_TEMPLATES : LINUX_TEMPLATES
      const template = templates.find((item) => item.id === templateId) || templates[0]
      const modules = (asset && asset.modules) || []

      // 资产变化时补齐默认模块、命名空间与模板。
      React.useEffect(() => {
        if (!asset) return
        if (asset.kind === 'k8s') {
          const configured = (asset.k8s && asset.k8s.namespaces) || []
          setNamespaceOptions(configured)
          setPodOptions([])
          setPod('')
          setNamespace((current) => (configured.includes(current) ? current : configured[0] || ''))
          if (!K8S_TEMPLATES.some((item) => item.id === templateId)) setTemplateId(K8S_TEMPLATES[0].id)
          return
        }
        if (!asset.modules.some((item) => item.name === moduleName)) {
          setModuleName(asset.modules.length ? asset.modules[0].name : '')
        }
        if (!LINUX_TEMPLATES.some((item) => item.id === templateId)) setTemplateId(LINUX_TEMPLATES[0].id)
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [assetKey, catalog])

      // 记住最近一次选择。
      React.useEffect(() => {
        if (!assetKey) return
        savePrefs({ assetKey, moduleName, templateId, day, namespace })
      }, [assetKey, moduleName, templateId, day, namespace])

      // 首次拿到连接信息（或后端值变化）时同步到表单；token 永远留空由用户重填。
      React.useEffect(() => {
        if (!connection) return
        setConnForm((current) => ({
          gatewayBaseUrl: current.gatewayBaseUrl || connection.gatewayBaseUrl || '',
          mcpUrl: current.mcpUrl || connection.mcpUrl || '',
          token: '',
        }))
      }, [connection && connection.gatewayBaseUrl, connection && connection.mcpUrl])

      const visibleAssets = assets.filter((item) => {
        if (envFilter !== 'all' && item.envId !== envFilter) return false
        if (!query.trim()) return true
        const needle = query.trim().toLowerCase()
        return (
          item.key.toLowerCase().includes(needle) ||
          item.description.toLowerCase().includes(needle) ||
          item.tags.join(' ').toLowerCase().includes(needle) ||
          item.modules.some((module) => module.name.toLowerCase().includes(needle))
        )
      })

      const promptText = buildPrompt({ asset, moduleName, template, keyword, day, customDay, namespace, pod })

      /** 把提示词写进输入框；编辑器拒绝时回退为整段草稿替换。 */
      function handleInsert() {
        if (!inputActions) {
          setNotice('当前没有可写入的输入框')
          return
        }
        try {
          const span = inputActions.captureInsertion()
          if (inputActions.insertText(promptText, span)) {
            setNotice('已插入到输入框，补一句问题即可发送')
            return
          }
        } catch {
          /* 落到下面的回退路径 */
        }
        try {
          inputActions.setDraft((draft ? draft + '\n' : '') + promptText)
          setNotice('已写入输入框')
        } catch (failure) {
          setNotice(`写入失败：${failure && failure.message ? failure.message : String(failure)}`)
        }
      }

      /** 直接发送：先写草稿，再在下一拍提交。 */
      function handleSend() {
        if (!inputActions) {
          setNotice('当前没有可写入的输入框')
          return
        }
        try {
          inputActions.setDraft(promptText)
        } catch (failure) {
          setNotice(`写入失败：${failure && failure.message ? failure.message : String(failure)}`)
          return
        }
        setNotice('已提交发送')
        window.setTimeout(() => {
          try {
            inputActions.submit()
          } catch (failure) {
            setNotice(`发送失败：${failure && failure.message ? failure.message : String(failure)}`)
          }
        }, 0)
      }

      /** 触发某个资产在 gateway 侧重新认证。 */
      async function handleReconnect(event, server) {
        if (event) event.stopPropagation()
        setBusy(`正在请求重连 ${server}…`)
        try {
          await callApi('connect', { server })
          setNotice(`已请求重连 ${server}。若需要密码/MFA，请到运行 gateway_host.py 的终端完成认证。`)
        } catch (failure) {
          setNotice(`重连失败：${failure.message || String(failure)}`)
        } finally {
          setBusy('')
        }
      }

      /** 重启 MCP 连接（MCP 工具被移除后的手动恢复手段）。 */
      async function handleRestartMcp() {
        setBusy('正在重启 MCP 连接…')
        try {
          const result = await callApi('mcp.restart')
          if (result.readyAfter) setNotice(`MCP 已恢复：${result.toolCount} 个工具可用`)
          else if (result.outcome === 'pending') setNotice('已触发重连，连接仍在进行；几秒后点「刷新」确认')
          else setNotice(`重连后工具仍未就绪（${result.outcome}）。检查 10.12.0.101:8000 可达与 .token 是否正确`)
        } catch (failure) {
          setNotice(`重连 MCP 失败：${failure.message || String(failure)}`)
        } finally {
          setBusy('')
          window.setTimeout(() => setRefreshTick((tick) => tick + 1), 1500)
        }
      }

      /** MCP 未就绪时自动探活一次，把「地址填错」和「只是没注册」区分开。 */
      React.useEffect(() => {
        const notReady = catalog && catalog.mcp && catalog.mcp.ready === false
        if (!notReady) {
          setMcpProbe(null)
          return undefined
        }
        let alive = true
        callApi('mcp.probe', {})
          .then((value) => {
            if (alive) setMcpProbe(value)
          })
          .catch((failure) => {
            if (!alive) return
            setMcpProbe({ ok: false, message: failure.message || String(failure) })
          })
        return () => {
          alive = false
        }
      }, [catalog && catalog.mcp && catalog.mcp.ready, refreshTick])

      /** 手动测试 MCP 端点（保存前也可用）。 */
      async function handleProbeMcp() {
        setBusy('正在测试 MCP 端点…')
        try {
          const result = await callApi('mcp.probe', {})
          setMcpProbe(result)
          setNotice(`${result.ok ? '✅ ' : '❌ '}${result.message}`)
        } catch (failure) {
          setNotice(`❌ ${failure.message || String(failure)}`)
        } finally {
          setBusy('')
        }
      }

      /** 试连当前表单里的地址与 token（不保存）。gateway 与 MCP 都会测。 */
      async function handleTestConnection() {
        setConnTesting('正在试连…')
        setConnNotice('')
        try {
          const result = await callApi('settings.test', {
            gatewayBaseUrl: connForm.gatewayBaseUrl,
            mcpUrl: connForm.mcpUrl,
            token: connForm.token,
          })
          setConnNotice(`${result.ok ? '✅ ' : '❌ '}${result.message}`)
        } catch (failure) {
          setConnNotice(`❌ ${failure.message || String(failure)}`)
        } finally {
          setConnTesting('')
        }
      }

      /** 保存连接设置；Host 半保存后会重启 MCP 行让新地址生效。 */
      async function handleSaveConnection() {
        setConnTesting('正在保存…')
        setConnNotice('')
        try {
          const saved = await callApi('settings.save', {
            gatewayBaseUrl: connForm.gatewayBaseUrl,
            mcpUrl: connForm.mcpUrl,
            token: connForm.token,
          })
          const mcpOk = saved && saved.mcp && saved.mcp.readyAfter
          setConnForm((current) => ({ ...current, token: '' }))
          setConnNotice(
            `✅ 已保存到 ${saved.settingsFile}`
            + (mcpOk ? `，MCP 已重连（${saved.mcp.toolCount} 个工具）` : '，MCP 未就绪，可点上方「重连 MCP」再试'),
          )
          setRefreshTick((tick) => tick + 1)
        } catch (failure) {
          setConnNotice(`❌ ${failure.message || String(failure)}`)
        } finally {
          setConnTesting('')
        }
      }

      /** 动态拉取命名空间列表。 */
      async function handleLoadNamespaces() {
        if (!asset) return
        setBusy('正在拉取命名空间…')
        try {
          const value = await callApi('k8s.namespaces', { server: asset.key })
          setNamespaceOptions(value.namespaces || [])
          if (value.namespaces && value.namespaces.length && !namespace) setNamespace(value.namespaces[0])
          setNotice(`已拉取 ${(value.namespaces || []).length} 个命名空间`)
        } catch (failure) {
          setNotice(`拉取命名空间失败：${failure.message || String(failure)}`)
        } finally {
          setBusy('')
        }
      }

      /** 动态拉取 Pod 列表。 */
      async function handleLoadPods() {
        if (!asset || !namespace) {
          setNotice('请先选择命名空间')
          return
        }
        setBusy('正在拉取 Pod…')
        try {
          const value = await callApi('k8s.pods', { server: asset.key, namespace })
          setPodOptions(value.pods || [])
          setNotice(`已拉取 ${(value.pods || []).length} 个 Pod`)
        } catch (failure) {
          setNotice(`拉取 Pod 失败：${failure.message || String(failure)}`)
        } finally {
          setBusy('')
        }
      }

      const gateway = catalog && catalog.gateway
      const mcp = catalog && catalog.mcp
      const envChips = [{ id: 'all', label: '全部' }].concat(environments.map((item) => ({ id: item.id, label: item.label })))

      return h(
        'div',
        { className: 'lc-root' },
        h('style', null, STYLE_TEXT),
        h(
          'div',
          { className: 'lc-head' },
          h('span', {
            className: 'lc-dot',
            style: {
              background: !gateway
                ? 'var(--dsw-alias-state-idle-primary)'
                : gateway.reachable
                  ? 'var(--dsw-alias-state-success-primary)'
                  : 'var(--dsw-alias-state-error-primary)',
            },
            title: gateway ? (gateway.reachable ? `gateway 已连接（${gateway.connected}/${gateway.total}）` : 'gateway 未运行') : '',
          }),
          h('span', { className: 'lc-head-title' }, '日志上下文'),
          h(
            'button',
            {
              type: 'button',
              className: 'lc-icon-btn',
              title: mcp
                ? mcp.ready
                  ? `MCP 工具已就绪（${mcp.toolCount} 个），点此强制重连`
                  : 'MCP 工具未就绪，点此重启连接'
                : '重启 MCP 连接',
              disabled: loading || busy !== '',
              onClick: handleRestartMcp,
            },
            mcp && mcp.ready === false ? '重连 MCP' : 'MCP',
          ),
          h(
            'button',
            {
              type: 'button',
              className: 'lc-icon-btn',
              title: '刷新资产与连接状态',
              disabled: loading,
              onClick: () => setRefreshTick((tick) => tick + 1),
            },
            loading ? '刷新中' : '刷新',
          ),
        ),
        h(
          'div',
          { className: 'lc-body' },
          mcp && mcp.ready === false
            ? h(
                'div',
                { className: 'lc-warn' },
                h(
                  'div',
                  { className: 'lc-warn-text' },
                  h('div', null, 'MCP 工具未就绪：模型暂时查不了日志。'),
                  // 把实际连接目标显示出来：地址填错时一眼能看出（最常见的失败原因）
                  connection && connection.mcpUrl
                    ? h('div', { className: 'lc-hint' }, `连接目标：${connection.mcpUrl}`)
                    : null,
                  mcpProbe && mcpProbe.ok === false
                    ? h('div', { className: 'lc-hint' }, `探活失败：${mcpProbe.message}`)
                    : null,
                  mcpProbe && mcpProbe.ok
                    ? h('div', { className: 'lc-hint' }, `端点可达（${mcpProbe.message}），但客户端未注册工具，试试点「重连 MCP」。`)
                    : null,
                ),
                h(
                  'div',
                  { style: { display: 'flex', flexDirection: 'column', gap: '4px' } },
                  h(
                    'button',
                    { type: 'button', className: 'lc-icon-btn', disabled: busy !== '', onClick: handleRestartMcp },
                    '重连 MCP',
                  ),
                  h(
                    'button',
                    { type: 'button', className: 'lc-icon-btn', disabled: busy !== '', onClick: handleProbeMcp, title: '直接向 MCP 地址发一次 initialize 握手，验证地址与 token' },
                    '测试端点',
                  ),
                ),
              )
            : null,
          error
            ? h(
                'div',
                { className: 'lc-error' },
                h('span', null, error),
                h('span', { className: 'lc-hint' }, '请确认 gateway_host.py 正在运行，并在项目目录下执行过 `uv run python create_token.py`。'),
                h(
                  'button',
                  { type: 'button', className: 'lc-btn', onClick: () => setRefreshTick((tick) => tick + 1) },
                  '重试',
                ),
              )
            : null,

          // 连接设置：地址与 token 都在这里配，不再需要改 patch 文件
          h(
            'div',
            { className: 'lc-group' },
            h(
              'div',
              { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
              h('span', { className: 'lc-label', style: { flex: '1' } }, '连接设置'),
              h(
                'button',
                {
                  type: 'button',
                  className: 'lc-icon-btn',
                  title: connection ? `设置文件：${connection.settingsFile}` : '配置网关地址与访问令牌',
                  onClick: () => setConnOpen((open) => !open),
                },
                connOpen ? '收起' : '配置',
              ),
            ),
            connOpen
              ? h(
                  'div',
                  { style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
                  h('input', {
                    className: 'lc-input',
                    placeholder: '网关地址，如 http://10.12.0.101:8765',
                    value: connForm.gatewayBaseUrl,
                    onChange: (event) => setConnForm((current) => ({ ...current, gatewayBaseUrl: event.target.value })),
                  }),
                  h('input', {
                    className: 'lc-input',
                    placeholder: 'MCP 地址，如 http://10.12.0.101:8000/mcp',
                    value: connForm.mcpUrl,
                    onChange: (event) => setConnForm((current) => ({ ...current, mcpUrl: event.target.value })),
                  }),
                  h('input', {
                    className: 'lc-input',
                    type: 'password',
                    placeholder: connection && connection.hasToken
                      ? `访问令牌（已设置 ${connection.tokenMasked}，留空则保持不变）`
                      : '访问令牌（local.token 的内容）',
                    value: connForm.token,
                    onChange: (event) => setConnForm((current) => ({ ...current, token: event.target.value })),
                  }),
                  h(
                    'div',
                    { className: 'lc-actions' },
                    h(
                      'button',
                      { type: 'button', className: 'lc-btn', disabled: connTesting !== '', onClick: handleTestConnection },
                      '试连',
                    ),
                    h(
                      'button',
                      {
                        type: 'button',
                        className: 'lc-btn',
                        'data-primary': 'true',
                        disabled: connTesting !== '',
                        onClick: handleSaveConnection,
                      },
                      '保存',
                    ),
                  ),
                  h('span', { className: 'lc-hint' }, busy || connTesting || connNotice
                    || (connection && connection.settingsFile ? `保存位置：${connection.settingsFile}` : '')),
                )
              : h(
                  'span',
                  { className: 'lc-hint' },
                  connection
                    ? `${connection.gatewayBaseUrl}${connection.hasToken ? ' · 令牌已设置' : ' · 未设置令牌'}`
                    : '未读取到连接信息',
                ),
          ),

          // 环境筛选
          h(
            'div',
            { className: 'lc-group' },
            h('span', { className: 'lc-label' }, '环境'),
            h(
              'div',
              { className: 'lc-chips' },
              envChips.map((chip) =>
                h(
                  'button',
                  {
                    key: chip.id,
                    type: 'button',
                    className: 'lc-chip',
                    'data-active': envFilter === chip.id ? 'true' : 'false',
                    onClick: () => setEnvFilter(chip.id),
                  },
                  chip.label,
                ),
              ),
            ),
          ),

          // 搜索
          h('input', {
            className: 'lc-input',
            placeholder: '搜索资产 / IP / 模块',
            value: query,
            onChange: (event) => setQuery(event.target.value),
          }),

          // 资产列表
          h(
            'div',
            { className: 'lc-group' },
            h('span', { className: 'lc-label' }, `资产（${visibleAssets.length}）`),
            visibleAssets.length === 0
              ? h('div', { className: 'lc-empty' }, '没有匹配的资产')
              : h(
                  'div',
                  { className: 'lc-list' },
                  visibleAssets.map((item) => {
                    const status = connectionOf(item)
                    return h(
                      'button',
                      {
                        key: item.key,
                        type: 'button',
                        className: 'lc-row',
                        'data-active': item.key === assetKey ? 'true' : 'false',
                        title: item.description,
                        onClick: () => setAssetKey(item.key),
                      },
                      h('span', { className: 'lc-dot', style: { background: status.color }, title: status.label }),
                      h(
                        'span',
                        { className: 'lc-row-main' },
                        h('span', { className: 'lc-row-name' }, item.key),
                        h(
                          'span',
                          { className: 'lc-row-sub' },
                          [status.label, item.kind === 'k8s' ? 'K8S' : null, item.tags.join(' / ') || item.description]
                            .filter(Boolean)
                            .join(' · '),
                        ),
                      ),
                    )
                  }),
                ),
          ),

          // 资产详情：模块或 K8S
          asset
            ? h(
                'div',
                { className: 'lc-group' },
                h(
                  'div',
                  { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
                  h('span', { className: 'lc-label', style: { flex: '1' } }, asset.kind === 'k8s' ? '命名空间 / Pod' : '模块'),
                  connectionOf(asset).label !== '已连接'
                    ? h(
                        'button',
                        {
                          type: 'button',
                          className: 'lc-icon-btn',
                          title: '触发 JumpServer 重新认证',
                          disabled: busy !== '',
                          onClick: (event) => handleReconnect(event, asset.key),
                        },
                        '重连',
                      )
                    : null,
                ),
                asset.kind === 'k8s'
                  ? h(
                      'div',
                      { style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
                      h(
                        'div',
                        { style: { display: 'flex', gap: '6px' } },
                        h(
                          'select',
                          {
                            className: 'lc-input',
                            value: namespace,
                            onChange: (event) => {
                              setNamespace(event.target.value)
                              setPod('')
                              setPodOptions([])
                            },
                          },
                          h('option', { value: '' }, '（选择命名空间）'),
                          namespaceOptions.map((item) => h('option', { key: item, value: item }, item)),
                        ),
                        h(
                          'button',
                          { type: 'button', className: 'lc-icon-btn', disabled: busy !== '', onClick: handleLoadNamespaces, title: '通过 kubectl 拉取命名空间' },
                          '拉取',
                        ),
                      ),
                      h(
                        'div',
                        { style: { display: 'flex', gap: '6px' } },
                        h('input', {
                          className: 'lc-input',
                          placeholder: 'Pod 名称（可选）',
                          value: pod,
                          list: 'lc-pod-options',
                          onChange: (event) => setPod(event.target.value),
                        }),
                        h('datalist', { id: 'lc-pod-options' }, podOptions.map((item) => h('option', { key: item, value: item }))),
                        h(
                          'button',
                          { type: 'button', className: 'lc-icon-btn', disabled: busy !== '', onClick: handleLoadPods, title: '通过 kubectl 拉取 Pod 列表' },
                          '拉取',
                        ),
                      ),
                    )
                  : h(
                      'div',
                      { className: 'lc-list' },
                      modules.length === 0
                        ? h('div', { className: 'lc-empty' }, '该资产未配置模块（可在 config/log_targets.yaml 中补充）')
                        : modules.map((module) =>
                            h(
                              'button',
                              {
                                key: module.name,
                                type: 'button',
                                className: 'lc-row',
                                'data-active': module.name === moduleName ? 'true' : 'false',
                                title: module.description,
                                onClick: () => setModuleName(module.name),
                              },
                              h(
                                'span',
                                { className: 'lc-row-main' },
                                h('span', { className: 'lc-row-name' }, module.name),
                                h('span', { className: 'lc-row-sub' }, module.logDir || module.description || '未配置日志目录'),
                              ),
                            ),
                          ),
                    ),
              )
            : null,

          // 模板 + 时间范围
          asset
            ? h(
                'div',
                { className: 'lc-group' },
                h('span', { className: 'lc-label' }, '动作模板'),
                h(
                  'div',
                  { className: 'lc-chips' },
                  templates.map((item) =>
                    h(
                      'button',
                      {
                        key: item.id,
                        type: 'button',
                        className: 'lc-chip',
                        'data-active': template && template.id === item.id ? 'true' : 'false',
                        onClick: () => setTemplateId(item.id),
                      },
                      item.label,
                    ),
                  ),
                ),
                template && template.day
                  ? h(
                      'div',
                      { className: 'lc-chips' },
                      DAY_OPTIONS.map((item) =>
                        h(
                          'button',
                          {
                            key: item.id,
                            type: 'button',
                            className: 'lc-chip',
                            'data-active': day === item.id ? 'true' : 'false',
                            onClick: () => setDay(item.id),
                          },
                          item.label,
                        ),
                      ),
                    )
                  : null,
                template && template.day && day === 'custom'
                  ? h('input', {
                      className: 'lc-input',
                      placeholder: 'YYYY-MM-DD',
                      value: customDay,
                      onChange: (event) => setCustomDay(event.target.value),
                    })
                  : null,
                template && template.keyword
                  ? h('input', {
                      className: 'lc-input',
                      placeholder: '要搜索的关键字',
                      value: keyword,
                      onChange: (event) => setKeyword(event.target.value),
                    })
                  : null,
              )
            : null,

          // 预览与动作
          h(
            'div',
            { className: 'lc-group' },
            h('span', { className: 'lc-label' }, '将插入的内容'),
            h(
              'div',
              { className: 'lc-readout' },
              h('textarea', {
                className: 'lc-preview',
                readOnly: true,
                value: promptText,
                spellCheck: false,
              }),
            ),
            h(
              'div',
              { className: 'lc-actions' },
              h('button', { type: 'button', className: 'lc-btn', 'data-primary': 'true', onClick: handleInsert }, '插入到输入框'),
              h('button', { type: 'button', className: 'lc-btn', onClick: handleSend }, '直接发送'),
            ),
            h('span', { className: 'lc-hint' }, '插入后补一句你的问题即可发送，例如「支付回调一直没生效」。'),
          ),

          h('span', { className: 'lc-notice' }, busy || notice),
        ),
      )
    }

    /** 输入框左侧的入口按钮：打开右栏标签。 */
    function makeOpenButton(sidebarRight) {
      return function OpenLogContextButton() {
        return h(
          React.Fragment,
          null,
          h('style', null, STYLE_TEXT),
          h(
            'button',
            {
              type: 'button',
              className: 'lc-open-btn',
              title: '打开日志上下文面板（选择资产 / 模块后一键填入查询条件）',
              onClick: () => {
                try {
                  sidebarRight.openTab(KIND)
                } catch (failure) {
                  console.error('[log-context] 打开右栏标签失败:', failure)
                }
              },
            },
            '日志上下文',
          ),
        )
      }
    }

    return {
      inject: ['slots'],
      // 开发期冒烟检查用的纯函数出口；Loader 只读取 inject / apply，忽略其余字段。
      internals: { buildPrompt, LINUX_TEMPLATES, K8S_TEMPLATES },
      apply(ctx) {
        ctx.inject(['sidebarRightTabs', 'sidebarRight'], (injected) => {
          const tabs = injected.get('sidebarRightTabs')
          const sidebarRight = injected.get('sidebarRight')
          if (!tabs || !sidebarRight) return undefined

          const disposers = []
          disposers.push(
            tabs.register({
              id: IMPL_ID,
              kind: KIND,
              keepMounted: true,
              priority: 'extension',
              title: () => '日志上下文',
              guide: [
                {
                  id: 'log-context',
                  order: 20,
                  title: () => '日志上下文',
                  description: () => '选择资产 / 模块后一键把查询上下文插入输入框',
                },
              ],
            }),
          )
          disposers.push(
            ctx.slots.inject('sidebar.right.pane.tab', () =>
              ctx.slots.register({ name: 'sidebar.right.pane.tab', key: IMPL_ID }, LogContextTab),
            ),
          )
          disposers.push(
            ctx.slots.inject('conversation.input.left', () =>
              ctx.slots.register(
                { name: 'conversation.input.left', id: 'log-context', order: 20 },
                makeOpenButton(sidebarRight),
              ),
            ),
          )
          return () => {
            for (const dispose of disposers.reverse()) {
              try {
                dispose()
              } catch (failure) {
                console.error('[log-context] 释放注册失败:', failure)
              }
            }
          }
        })
      },
    }
  },
})
