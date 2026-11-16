import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/userinsight'
const columns = [
  "报告编号",
  "分析主题",
  "目标人群",
  "关键指标",
  "核心发现",
  "数据区间",
  "生成时间",
  "下次生成"
]
const actions = [
  "提交生成",
  "发布报告",
  "归档报告"
]
const statuses = [
  "草稿",
  "生成中",
  "已发布",
  "已归档"
]
const stats = [
  {
    "label": "洞察报告总数",
    "value": 0
  },
  {
    "label": "已发布报告",
    "value": 0
  },
  {
    "label": "生成中报告",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type SharedSnapshot = {
  名称: string
  版本: number
  生成时间: string
  报告总数: number
  已发布数: number
}

export default function UserinsightView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [snapshot, setSnapshot] = useState<SharedSnapshot | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('洞察报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察列表读取失败')
    }
  }, [filters])

  const reloadSnapshot = useCallback(async () => {
    try {
      setSnapshot(await fetchJson<SharedSnapshot>(`${ENDPOINT}/snapshot`))
    } catch {
      // 快照读取失败不影响列表本身，页脚会提示列表错误。
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadSnapshot()
  }, [reloadSnapshot])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('用户洞察动作未生效，请稍后重试')
      }
      await reload()
      await reloadSnapshot()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察操作失败')
    }
  }

  return (
    <section className="page" data-module="userinsight">
      <header className="page-head">
        <div>
          <h2>用户洞察管理</h2>
          <p className="page-desc">管理自动生成的用户行为洞察报告，汇总关键指标变化和趋势分析。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('洞察报告登记入口尚未接入审批流')}>
            登记洞察报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出用户洞察清单
          </button>
        </div>
      </header>

      <div className="toolbar">
        <span className="hint-text">
          {snapshot
            ? `共享快照：${snapshot.名称} v${snapshot.版本} · 已发布 ${snapshot.已发布数} / 共 ${snapshot.报告总数} 篇 · 生成于 ${snapshot.生成时间}`
            : '共享快照：读取中'}
          {' '}订阅推送的“洞察快报”读取同一份快照，发布或归档报告后两边一起刷新。
        </span>
      </div>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无用户洞察数据，可先登记洞察报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条用户洞察记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
