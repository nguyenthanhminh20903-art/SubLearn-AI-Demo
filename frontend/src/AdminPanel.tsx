import {useEffect, useState, type FormEvent} from 'react'
import {JobSteps, type JobStatus} from './JobDetails'

const sections = {
  users: 'Tài khoản', models: 'Mô hình', jobs: 'Tác vụ', resources: 'Tài nguyên', experiments: 'Thực nghiệm',
} as const
type Section = keyof typeof sections
type RecordRow = Record<string, string | number | boolean | null>
type Editor = {kind: 'model' | 'experiment' | 'metrics' | 'run'; row?: RecordRow}
const modelTypes = ['ASR', 'TRANSLATION', 'ALIGNMENT', 'DIALECT', 'VAD', 'DIARIZATION', 'DENOISE']
const environments = ['LOCAL', 'COLAB', 'GPU_SERVER']

function parseObject(value: FormDataEntryValue | null) {
  const result = JSON.parse(String(value || '{}'))
  if (!result || Array.isArray(result) || typeof result !== 'object') {
    throw new Error('Cấu hình phải là một đối tượng JSON.')
  }
  return result
}

export default function AdminPanel({api}: {api: string}) {
  const [section, setSection] = useState<Section>('users')
  const [rows, setRows] = useState<RecordRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [version, setVersion] = useState(0)
  const [experiment, setExperiment] = useState<RecordRow[] | null>(null)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [saving, setSaving] = useState(false)
  const [detail, setDetail] = useState<JobStatus | null>(null)
  const [runtime, setRuntime] = useState('')
  const [status, setStatus] = useState('')
  const [environment, setEnvironment] = useState('')
  const [resourceJob, setResourceJob] = useState('')
  const [report, setReport] = useState('')

  async function request(path: string, options?: RequestInit) {
    const response = await fetch(`${api}/admin/${path}`, {credentials: 'include', ...options})
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Không thực hiện được thao tác.')
    return payload
  }

  useEffect(() => {
    let ignore = false
    setRows([])
    setError('')
    setLoading(true)
    setExperiment(null)
    setEditor(null)
    setDetail(null)
    setRuntime('')
    const filters = new URLSearchParams()
    if ((section === 'jobs' || section === 'resources') && environment) filters.set('environment', environment)
    if (section === 'jobs' && status) filters.set('status', status)
    if (section === 'resources' && resourceJob) filters.set('job_id', resourceJob)
    request(section + (filters.size ? '?' + filters.toString() : '')).then(data => {if (!ignore) setRows(data)})
      .catch(failure => {if (!ignore) setError(failure.message)})
      .finally(() => {if (!ignore) setLoading(false)})
    return () => {ignore = true}
  }, [section, version, api, status, environment, resourceJob])

  async function action(path: string, method: string, body?: Record<string, unknown>) {
    setError('')
    try {
      await request(path, {method, ...(body && {
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      })})
      setVersion(value => value + 1)
    } catch (failure) {
      setError((failure as Error).message)
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!editor || saving) return
    const fields = new FormData(event.currentTarget)
    const values = Object.fromEntries(fields)
    setError('')
    setSaving(true)
    try {
      let path = 'experiments'
      let method = 'POST'
      let body: unknown
      if (editor.kind === 'model') {
        path = editor.row ? `models/${editor.row.model_id}` : 'models'
        if (editor.row) method = 'PATCH'
        const settings = parseObject(fields.get('settings'))
        if (fields.has('pipeline_default')) settings.pipeline_default = true
        else delete settings.pipeline_default
        body = {...values, is_active: fields.has('is_active'), settings}
      } else if (editor.kind === 'metrics') {
        path = `experiments/${editor.row?.experiment_id}/results`
        body = JSON.parse(String(fields.get('metrics')))
        if (!Array.isArray(body) || body.length === 0) throw new Error('Nhập một mảng JSON có ít nhất một số đo.')
      } else if (editor.kind === 'run') {
        path = `experiments/${editor.row?.experiment_id}/runs`
        const payload = parseObject(fields.get('report'))
        const numericMetrics = Object.fromEntries(Object.entries(payload.metrics ?? {})
          .filter(([name, value]) => typeof value === 'number' && name !== 'count'))
        if (!Object.keys(numericMetrics).length) throw new Error('Báo cáo cần có ít nhất một số đo trong trường metrics.')
        body = {
          video_id: fields.get('video_id'), model_id: fields.get('model_id') || null,
          test_group: fields.get('test_group'), execution_environment: fields.get('environment'),
          dataset: payload.dataset, dataset_version: payload.dataset_version,
          configuration: payload.configuration ?? {}, metrics: numericMetrics,
        }
      } else {
        body = {...values, configuration: parseObject(fields.get('configuration'))}
      }
      await request(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      setEditor(null)
      if (editor.kind === 'metrics' || editor.kind === 'run') {
        setExperiment(await request(`experiments/${editor.row?.experiment_id}/results`))
      }
      else setVersion(value => value + 1)
    } catch (failure) {
      setError((failure as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const columns = {
    users: ['username', 'email', 'role', 'status'],
    models: ['model_name', 'model_type', 'model_version', 'execution_environment', 'quantization', 'is_active'],
    jobs: ['job_id', 'title', 'job_type', 'status', 'progress', 'retry_of_job_id', 'error_message'],
    resources: ['recorded_at', 'job_id', 'environment', 'worker_id', 'cpu_usage', 'ram_usage_mb', 'gpu_usage', 'vram_usage_mb', 'disk_usage_mb'],
    experiments: ['experiment_id', 'experiment_name', 'dataset_name', 'dataset_version'],
  }[section]

  return <div className="content">
    <h1 className="mb-4 text-2xl font-bold">Quản trị</h1>
    <nav aria-label="Quản trị" className="mb-4 flex flex-wrap gap-2">
      {Object.entries(sections).map(([key, label]) =>
        <button key={key} className={section === key ? 'primary' : 'secondary'}
          disabled={saving} onClick={() => setSection(key as Section)}>{label}</button>)}
    </nav>
    <div className="mb-3 flex gap-2">
      {section === 'models' && <button className="primary small" disabled={saving} onClick={() => {
        setEditor({kind: 'model'}); setError('')
      }}>Thêm cấu hình mô hình</button>}
      {section === 'experiments' && <button className="primary small" disabled={saving} onClick={() => {
        setEditor({kind: 'experiment'}); setError('')
      }}>Tạo thực nghiệm</button>}
      {(section === 'jobs' || section === 'resources') &&
        <button className="secondary small" disabled={loading} onClick={() => setVersion(value => value + 1)}>Làm mới</button>}
    </div>
    {(section === 'jobs' || section === 'resources') && <div className="control-form mb-3 flex flex-wrap gap-2">
      <label className="grid">Môi trường<select value={environment} onChange={event => setEnvironment(event.target.value)}>
        <option value="">Tất cả</option>
        {environments.map(value => <option key={value}>{value}</option>)}
      </select></label>
      {section === 'jobs' ? <label className="grid">Trạng thái<select value={status} onChange={event => setStatus(event.target.value)}>
        <option value="">Tất cả</option>
        {['PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'].map(value => <option key={value}>{value}</option>)}
      </select></label> : <label className="grid">Mã tác vụ<input value={resourceJob} inputMode="numeric"
        onChange={event => setResourceJob(event.target.value.replace(/[^0-9]/g, ''))}/></label>}
    </div>}
    {editor && <form className="control-form mb-4 grid max-w-2xl gap-3 rounded-xl bg-white p-4"
      key={`${editor.kind}-${editor.row?.model_id ?? editor.row?.experiment_id ?? 'new'}`} onSubmit={save}>
      <fieldset className="grid gap-2" disabled={saving}>
        <legend className="mb-2 font-semibold">
          {editor.kind === 'model' ? (editor.row ? 'Sửa cấu hình mô hình' : 'Cấu hình mô hình mới') :
            editor.kind === 'experiment' ? 'Thực nghiệm mới' : `Nhập kết quả · ${editor.row?.experiment_name}`}
        </legend>
        {editor.kind === 'model' && <>
          <label className="grid">Tên mô hình<input name="model_name" required maxLength={255}
            defaultValue={String(editor.row?.model_name ?? '')}/></label>
          <label className="grid">Phiên bản<input name="model_version" required maxLength={100}
            defaultValue={String(editor.row?.model_version ?? '')}/></label>
          <label className="grid">Loại mô hình<select name="model_type" defaultValue={String(editor.row?.model_type ?? 'ASR')}>
            {modelTypes.map(value => <option key={value}>{value}</option>)}
          </select></label>
          <label className="grid">Môi trường<select name="execution_environment"
            defaultValue={String(editor.row?.execution_environment ?? 'LOCAL')}>
            {environments.map(value => <option key={value}>{value}</option>)}
          </select></label>
          <label className="grid">Đường dẫn checkpoint<input name="model_path" maxLength={4000}
            defaultValue={String(editor.row?.model_path ?? '')}/></label>
          <label className="grid">Lượng tử hóa<input name="quantization" maxLength={20}
            defaultValue={String(editor.row?.quantization ?? '')}/></label>
          <label className="grid">Tham số JSON<textarea name="settings" rows={3}
            defaultValue={typeof editor.row?.settings === 'string' ? editor.row.settings : JSON.stringify(editor.row?.settings ?? {})}/></label>
          <label className="flex items-center gap-2"><input type="checkbox" name="is_active"
            defaultChecked={Boolean(editor.row?.is_active)}/> Đang hoạt động</label>
          <label className="flex items-center gap-2"><input type="checkbox" name="pipeline_default"
            defaultChecked={typeof editor.row?.settings === 'string' &&
              JSON.parse(editor.row.settings)?.pipeline_default === true}/> Dùng cho tác vụ nhận dạng Local</label>
        </>}
        {editor.kind === 'experiment' && <>
          <label className="grid">Tên thực nghiệm<input name="experiment_name" required maxLength={255}/></label>
          <label className="grid">Tập dữ liệu<input name="dataset_name" required maxLength={255}/></label>
          <label className="grid">Phiên bản tập dữ liệu<input name="dataset_version" required maxLength={100}/></label>
          <label className="grid">Đường dẫn dữ liệu tham chiếu<input name="reference_path" required maxLength={4000}/></label>
          <label className="grid">Cấu hình JSON<textarea name="configuration" rows={3} defaultValue="{}"/></label>
        </>}
        {editor.kind === 'metrics' && <label className="grid">Số đo từ các tác vụ đã hoàn thành (JSON)
          <textarea name="metrics" rows={6} required spellCheck={false}
            placeholder={'[{"job_id":123,"metric_name":"WER","metric_value":0.2,"test_group":"baseline"}]'}/>
        </label>}
        {editor.kind === 'run' && <>
          <label className="grid">Mã video thử nghiệm<input name="video_id" inputMode="numeric" pattern="[1-9][0-9]*" required/></label>
          <label className="grid">Mã mô hình<input name="model_id" inputMode="numeric" pattern="[1-9][0-9]*"/></label>
          <label className="grid">Nhóm so sánh<input name="test_group" required maxLength={100}/></label>
          <label className="grid">Môi trường<select name="environment" defaultValue="COLAB">
            {environments.map(value => <option key={value}>{value}</option>)}
          </select></label>
          <label className="grid">Tệp báo cáo JSON<input type="file" accept=".json,application/json" onChange={event => {
            const file = event.target.files?.[0]
            if (file) void file.text().then(setReport).catch(() => setError('Không đọc được báo cáo.'))
          }}/></label>
          <label className="grid">Nội dung báo cáo<textarea name="report" required rows={6} spellCheck={false}
            value={report} onChange={event => setReport(event.target.value)}/></label>
        </>}
      </fieldset>
      <div className="flex gap-2">
        <button type="submit" className="primary small" disabled={saving}>{saving ? 'Đang lưu…' : 'Lưu'}</button>
        <button type="button" className="secondary small" disabled={saving} onClick={() => setEditor(null)}>Hủy</button>
      </div>
    </form>}
    {error && <p className="modal-error" role="alert">{error}</p>}
    {runtime && <p className="mb-3 rounded-lg bg-white p-3" role="status">{runtime}</p>}
    {detail && <JobSteps job={detail}/>}
    {loading ? <p role="status">Đang tải…</p> : rows.length === 0 ? <p>Chưa có dữ liệu.</p> :
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0 rounded-xl bg-white text-left text-sm">
          <thead><tr>{columns.map(column => <th key={column} className="px-3 py-2">{column}</th>)}<th scope="col"/></tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>
            {columns.map(column => <td key={column} className="border-t border-slate-100 px-3 py-2">
              {(column === 'cpu_usage' || column === 'gpu_usage') && row[column] != null &&
                <meter className="mr-2 w-16" min={0} max={100} value={Number(row[column])} aria-label={column}/>}
              {String(row[column] ?? '—')}
            </td>)}
            <td className="border-t border-slate-100 px-3 py-2">
              {section === 'users' && <div className="flex flex-wrap items-center gap-2">
                <select aria-label={`Vai trò của ${row.username}`} value={String(row.role)} onChange={event => {
                  const role = event.target.value
                  if (role === 'ADMIN' || confirm(`Đổi vai trò ${row.username} thành Người dùng?`)) {
                    void action(`users/${row.user_id}`, 'PATCH', {role})
                  }
                }}><option value="USER">Người dùng</option><option value="ADMIN">Quản trị viên</option></select>
                <button className="secondary small" onClick={() => {
                  const status = row.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
                  if (status === 'ACTIVE' || confirm(`Khóa tài khoản ${row.username}?`)) {
                    void action(`users/${row.user_id}`, 'PATCH', {status})
                  }
                }}>{row.status === 'ACTIVE' ? 'Khóa' : 'Mở khóa'}</button>
              </div>}
              {section === 'jobs' && (row.status === 'PENDING' || row.status === 'RUNNING') &&
                <button className="secondary small" onClick={() => {
                  if (confirm(`Hủy tác vụ #${row.job_id}?`)) void action(`jobs/${row.job_id}/cancel`, 'POST')
                }}>Hủy</button>}
              {section === 'jobs' && row.status === 'FAILED' && (row.job_type === 'TRANSCRIBE' || row.job_type === 'TRANSLATE') &&
                <button className="secondary small" onClick={() => action(`jobs/${row.job_id}/retry`, 'POST')}>Thử lại</button>}
              {section === 'jobs' && <button className="secondary small" onClick={() => {
                request(`jobs/${row.job_id}`).then(setDetail).catch(failure => setError(failure.message))
              }}>Chi tiết</button>}
              {section === 'models' && <button className="secondary small" disabled={saving} onClick={() => {
                setEditor({kind: 'model', row}); setError('')
              }}>Chỉnh sửa</button>}
              {section === 'models' && <button className="secondary small" onClick={() => {
                request(`models/${row.model_id}/runtime`).then(result => setRuntime(`${row.model_name} · ${result.detail}`))
                  .catch(failure => setError(failure.message))
              }}>Kiểm tra môi trường</button>}
              {section === 'experiments' && <div className="flex gap-2"><button className="secondary small" onClick={() => {
                request(`experiments/${row.experiment_id}/results`).then(setExperiment)
                  .catch(failure => setError(failure.message))
              }}>Kết quả</button><button className="secondary small" disabled={saving} onClick={() => {
                setEditor({kind: 'metrics', row}); setError('')
              }}>Nhập số đo</button><button className="secondary small" disabled={saving} onClick={() => {
                setReport(''); setEditor({kind: 'run', row}); setError('')
              }}>Nhập báo cáo</button></div>}
            </td>
          </tr>)}</tbody>
        </table>
      </div>}
    {experiment && <section className="mt-4 rounded-xl bg-white p-4" aria-label="Kết quả thực nghiệm">
      {experiment.length === 0 ? <p>Chưa có kết quả.</p> : experiment.map((row, index) =>
        <p key={index} className="py-1">{row.test_group} · {row.metric_name} · {row.metric_value} · Tác vụ #{row.job_id}
          {row.models && ` · ${row.models}`}</p>)}
    </section>}
  </div>
}
