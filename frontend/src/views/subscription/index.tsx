import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/subscription'
const columns = [
  "订阅编号",
  "订阅内容",
  "接收人",
  "推送渠道",
  "推送频率",
  "数据粒度",
  "静默时段",
  "下次推送时间"
]
const actions = [
  "提交订阅评审",
  "启用订阅",
  "暂停订阅"
]
const pushStatuses = [
  "待合并",
  "已合并",
  "静默暂存",
  "投递中",
  "已投递",
  "已合并投递",
  "投递失败"
]

type Row = Record<string, string | number | null>
type Dataset = {
  key: string
  名称: string
  摘要: string
  条目数: number
  重要条目数: number
}

type TimelineBatch = Record<string, unknown>

type Timeline = {
  generated_at: string
  snapshot_version: number
  snapshot_generated_at: string
  datasets: Dataset[]
  overview: Record<string, number>
  failures: Record<string, unknown>[]
  batches: TimelineBatch[]
}

type PushSummary = {
  at: string
  generated: number
  delivered: number
  held: number
  failed: number
  released: number
  batch_ids: string[]
  skipped: { 订阅编号: string; 原因: string }[]
}

const emptyTimeline: Timeline = {
  generated_at: '',
  snapshot_version: 0,
  snapshot_generated_at: '',
  datasets: [],
  overview: {},
  failures: [],
  batches: [],
}

const overviewLabels: [string, string][] = [
  ["总批次", "推送批次"],
  ["已投递", "已投递"],
  ["合并投递", "合并投递"],
  ["静默暂存", "静默暂存"],
  ["投递失败", "投递失败"],
  ["待投递", "待投递"],
  ["重要告警突破静默", "突破静默"],
]

function formatCell(value: unknown) {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function localNow() {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
}

export default function SubscriptionView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [timeline, setTimeline] = useState<Timeline>(emptyTimeline)
  const [pushAt, setPushAt] = useState(localNow)
  const [recordStatus, setRecordStatus] = useState('')
  const [selectedCode, setSelectedCode] = useState('')
  const [summary, setSummary] = useState<PushSummary | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`)
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送列表读取失败')
    }
  }, [filters])

  const reloadTimeline = useCallback(async () => {
    try {
      setTimeline(await fetchJson<Timeline>(`${ENDPOINT}/timeline?limit=80`))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '推送时间线读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadTimeline()
  }, [reloadTimeline])

  const enabledCount = rows.filter((row) => row.status === '已启用').length
  const reviewingCount = rows.filter((row) => row.status === '订阅评审中').length
  const statCards = [
    { label: '订阅配置总数', value: total },
    { label: '已启用订阅', value: enabledCount },
    { label: '评审中订阅', value: reviewingCount },
    { label: '待推送分析数据集', value: timeline.datasets.length },
  ]

  const visibleBatches = useMemo(
    () => timeline.batches.filter((batch) => !recordStatus || batch.status === recordStatus),
    [timeline.batches, recordStatus],
  )
  const selectedBatch = useMemo(
    () => timeline.batches.find((batch) => String(batch.批次号) === selectedCode) ?? null,
    [timeline.batches, selectedCode],
  )

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '订阅推送动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? `${action}已生效`)
      await reload()
      await reloadTimeline()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送操作失败')
    }
  }

  async function runPush() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/push`, {
        method: 'POST',
        body: JSON.stringify({ values: { at: pushAt } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '合并推送未执行成功')
      }
      setSummary(payload.entry as PushSummary)
      setNotice(payload.message ?? '合并推送已执行')
      await reload()
      await reloadTimeline()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '合并推送执行失败')
    }
  }

  return (
    <section className="page" data-module="subscription">
      <header className="page-head">
        <div>
          <h2>订阅推送管理</h2>
          <p className="page-desc">
            按“接收人 + 推送渠道 + 推送频率 + 原定推送时间”合并多频率订阅，一次投递只落一条可追溯记录；
            静默结束集中释放，暂停后下次推送整体顺延。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void runPush()}>
            执行合并推送
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出订阅推送清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <div className="toolbar">
        <label className="filter-item">
          <span>模拟推送时刻</span>
          <input value={pushAt} onChange={(event) => setPushAt(event.target.value)} placeholder="YYYY-MM-DD HH:MM" />
        </label>
        <span className="hint-text">
          到期的排期会按频率合并；时刻可回填用于补推验证，缺省为当前时间。
        </span>
      </div>

      <section className="panel">
        <header className="panel-head">
          <h3>待推送分析数据集</h3>
          <span className="hint-text">
            洞察快照 v{timeline.snapshot_version}（{formatCell(timeline.snapshot_generated_at)}），
            与用户洞察页读取同一份快照
          </span>
        </header>
        <div className="dataset-row">
          {timeline.datasets.map((dataset) => (
            <article key={dataset.key} className="dataset-card">
              <strong>{dataset.名称}</strong>
              <span className="stat-label">{dataset.摘要}</span>
              <span className="dataset-meta">
                条目 {dataset.条目数} · 重要 {dataset.重要条目数}
              </span>
            </article>
          ))}
          {timeline.datasets.length === 0 && <span className="empty-state">暂无待推送数据集</span>}
        </div>
      </section>

      {summary && (
        <section className="panel">
          <header className="panel-head">
            <h3>本次合并推送结果</h3>
            <span className="hint-text">{summary.at} 执行</span>
          </header>
          <div className="stat-row">
            {[
              { label: '生成批次', value: summary.generated },
              { label: '直接投递', value: summary.delivered },
              { label: '静默暂存', value: summary.held },
              { label: '投递失败', value: summary.failed },
              { label: '释放暂存', value: summary.released },
            ].map((item) => (
              <article key={item.label} className="stat-card">
                <span className="stat-label">{item.label}</span>
                <strong className="stat-value">{item.value}</strong>
              </article>
            ))}
          </div>
          <p className="hint-text">
            批次：{summary.batch_ids.length ? summary.batch_ids.join('、') : '无新增批次（该批次已存在，不重复投递）'}
          </p>
          {summary.skipped.length > 0 && (
            <p className="error-text">
              跳过配置：{summary.skipped.map((item) => `${item.订阅编号}（${item.原因}）`).join('；')}
            </p>
          )}
        </section>
      )}

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
              <td className="empty-state" colSpan={columns.length + 1}>暂无订阅推送数据，可先登记订阅配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="panel" data-module="subscription-timeline">
        <header className="panel-head">
          <h3>推送时间线</h3>
          <span className="hint-text">每个推送批次一条，按原定推送时间倒序</span>
        </header>

        <div className="stat-row">
          {overviewLabels.map(([key, label]) => (
            <article key={key} className="stat-card">
              <span className="stat-label">{label}</span>
              <strong className="stat-value">{timeline.overview[key] ?? 0}</strong>
            </article>
          ))}
        </div>

        {timeline.failures.length > 0 && (
          <div className="failure-list">
            <h4>失败项（{timeline.failures.length}）</h4>
            <ul>
              {timeline.failures.map((failure) => (
                <li key={String(failure.批次号)}>
                  <button
                    className="link"
                    type="button"
                    onClick={() => setSelectedCode(String(failure.批次号))}
                  >
                    {formatCell(failure.批次号)}
                  </button>
                  <span> {formatCell(failure.接收人)} / {formatCell(failure.推送渠道)}</span>
                  <span> · {formatCell(failure.原定推送时间)}</span>
                  <span className="error-text"> · {formatCell(failure.失败原因)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="filter-bar">
          <label className="filter-item">
            <span>批次状态</span>
            <select value={recordStatus} onChange={(event) => setRecordStatus(event.target.value)}>
              <option value="">全部状态</option>
              {pushStatuses.map((status) => <option key={status} value={status}>{status}</option>)}
            </select>
          </label>
        </div>

        <ol className="timeline">
          {visibleBatches.map((batch) => (
            <li key={String(batch.批次号)} className={`timeline-item stage-${String(batch.status)}`}>
              <div className="timeline-dot" />
              <div className="timeline-body">
                <div className="timeline-head">
                  <button className="link" type="button" onClick={() => setSelectedCode(String(batch.批次号))}>
                    {formatCell(batch.批次号)}
                  </button>
                  <span className={`tag tag-${String(batch.status)}`}>{formatCell(batch.status)}</span>
                  <span className="hint-text">{formatCell(batch.原定推送时间)} · {formatCell(batch.推送频率)}</span>
                </div>
                <div className="timeline-title">{formatCell(batch.消息标题)}</div>
                <div className="timeline-meta">
                  <span>{formatCell(batch.接收人)} / {formatCell(batch.推送渠道)}</span>
                  <span>内容：{formatCell(batch.订阅内容)}</span>
                  <span>静默判定：{formatCell(batch.静默判定)}</span>
                  <span>投递结果：{formatCell(batch.投递结果)}</span>
                </div>
                <div className="timeline-meta">
                  <span>下次推送：{formatCell(batch.下次推送时间)}</span>
                  <span>合并主批次：{formatCell(batch.合并主批次)}</span>
                  {batch.失败原因 ? <span className="error-text">失败原因：{formatCell(batch.失败原因)}</span> : null}
                </div>
              </div>
            </li>
          ))}
          {visibleBatches.length === 0 && (
            <li className="empty-state">当前筛选下没有推送批次，点击“执行合并推送”生成</li>
          )}
        </ol>

        {selectedBatch && (
          <div className="detail-panel">
            <header className="panel-head">
              <h4>批次明细 {formatCell(selectedBatch.批次号)}</h4>
              <button className="btn ghost" type="button" onClick={() => setSelectedCode('')}>关闭</button>
            </header>
            <dl className="detail-grid">
              {([
                ['接收人', selectedBatch.接收人],
                ['推送渠道', selectedBatch.推送渠道],
                ['推送频率', selectedBatch.推送频率],
                ['批次窗口', selectedBatch.批次窗口],
                ['订阅编号', selectedBatch.订阅编号],
                ['订阅内容', selectedBatch.订阅内容],
                ['优先级', selectedBatch.优先级],
                ['优先级裁决', selectedBatch.优先级裁决],
                ['静默判定', selectedBatch.静默判定],
                ['静默时段', selectedBatch.静默时段],
                ['静默结束时间', selectedBatch.静默结束时间],
                ['原定推送时间', selectedBatch.原定推送时间],
                ['实际投递时间', selectedBatch.实际投递时间],
                ['下次推送时间', selectedBatch.下次推送时间],
                ['投递结果', selectedBatch.投递结果],
                ['合并主批次', selectedBatch.合并主批次],
                ['合并批次列表', Array.isArray(selectedBatch.合并批次列表) ? (selectedBatch.合并批次列表 as string[]).join('、') : ''],
                ['洞察快照版本', selectedBatch.洞察快照版本],
                ['分析数据集', selectedBatch.分析数据集],
                ['处理轨迹', ((selectedBatch.处理轨迹 as { 阶段: string; 时间: string }[]) ?? []).map((step) => `${step.阶段}@${step.时间}`).join(' → ')],
              ] as [string, unknown][]).map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{formatCell(value)}</dd>
                </div>
              ))}
            </dl>
            <h5>消息条目</h5>
            <ul className="entry-list">
              {((selectedBatch.消息条目 as Record<string, string>[]) ?? []).map((entry, index) => (
                <li key={`${entry.标题}-${index}`}>
                  <span className={`tag tag-${entry.重要性 === '重要' ? '投递失败' : '已投递'}`}>{entry.重要性}</span>
                  <strong>{entry.标题}</strong>
                  <span className="hint-text">（{entry.来源} · {entry.订阅内容}）</span>
                  <div>{entry.摘要}</div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <footer className="page-foot">
        <span>共 {total} 条订阅配置 · {timeline.batches.length} 个推送批次</span>
        <span>
          {notice && <span className="ok-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
