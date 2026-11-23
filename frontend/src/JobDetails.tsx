export const stepLabels: Record<string, string> = {
  PREPARE_AUDIO: 'Chuẩn bị nguồn và âm thanh',
  TRANSCRIBE: 'Nhận dạng lời thoại',
  OPTIMIZE: 'Căn chỉnh và phân đoạn',
  TRANSLATE: 'Dịch phụ đề',
  LIVE_ASR: 'Nhận dạng trực tiếp',
  LIVE_TRANSLATE: 'Dịch đoạn trực tiếp',
  ANALYZE_AUDIO: 'Phân tích tiếng nói',
  EVALUATE: 'Thực nghiệm',
}
const audioLabels: Record<string, string> = {RAW: 'Âm thanh gốc', NORMALIZED: 'Chuẩn hóa', DENOISED: 'Khử nhiễu'}
export const dialectLabels: Record<string, string> = {
  NORTHERN: 'Giọng Bắc', CENTRAL: 'Giọng Trung', SOUTHERN: 'Giọng Nam', UNKNOWN: 'Chưa xác định vùng giọng',
}
const dialectStates: Record<string, string> = {
  completed: 'Đã phân loại vùng giọng', not_configured: 'Chưa có mô hình vùng giọng',
  uncertain: 'Điểm dự đoán vùng giọng chưa đủ tin cậy',
  insufficient_speech: 'Chưa đủ lời thoại rõ để phân loại vùng giọng', unavailable: 'Mô hình vùng giọng chưa chạy được',
}

const jobLabels: Record<string, string> = {
  PENDING: 'Đang chờ', RUNNING: 'Đang xử lý', FAILED: 'Thất bại',
  COMPLETED: 'Hoàn tất', CANCELLED: 'Đã hủy',
}

export type JobStatus = {
  job_id: string | number
  job_type: string
  status: string
  progress: number
  error_message?: string
  audio_assets?: {audio_type: string; sample_rate: number; channels: number; duration_ms: number}[]
  dialects?: {dialect_label: string; intervals: number; duration_ms?: number | string}[]
  steps: {
    step_type: string
    execution_environment: string
    status: string
    error_message?: string
    model_name?: string
    metrics?: Record<string, unknown>
  }[]
}

export function JobSteps({job, onCancel}: {job: JobStatus; onCancel?: () => void}) {
  return <section className="mb-3 rounded-xl bg-white p-4 text-sm" aria-label="Tiến trình tác vụ">
    <div className="flex flex-wrap items-center gap-3">
      <b>Tác vụ #{job.job_id}</b><span>{jobLabels[job.status] || job.status}</span>
      {(job.status === 'PENDING' || job.status === 'RUNNING') && onCancel &&
        <button type="button" className="secondary small" onClick={onCancel}>Hủy tác vụ</button>}
      <progress max={100} value={job.progress} aria-label="Phần trăm hoàn thành"/>
      <span>{job.progress}%</span>
    </div>
    {job.error_message && <p className="modal-error">{job.error_message}</p>}
    <ul className="mt-2 grid gap-2">
      {job.steps.map((step, index) => <li key={index}>
        <span className="font-medium">{stepLabels[step.step_type] || step.step_type}</span>
        {' · '}{job.status === 'FAILED' && step.status === 'CANCELLED' ? 'Chưa chạy' : jobLabels[step.status] || step.status}{' · '}{step.execution_environment}
        {(step.model_name || typeof step.metrics?.model === 'string') &&
          <span> · {step.model_name || String(step.metrics?.model)}</span>}
        {step.metrics?.detector != null && <span> · {String(step.metrics.detector)}</span>}
        {Number(step.metrics?.speakers) > 0 && <span> · {String(step.metrics?.speakers)} người nói</span>}
        {typeof step.metrics?.dialect_status === 'string' && dialectStates[step.metrics.dialect_status] &&
          <span> · {dialectStates[step.metrics.dialect_status]}</span>}
        {step.metrics?.denoise === 'failed' && <span> · Đã dùng âm thanh chuẩn hóa sau lỗi khử nhiễu</span>}
        {step.error_message && <p className="modal-error">{step.error_message}</p>}
      </li>)}
    </ul>
    {!!job.audio_assets?.length && <div className="mt-2 flex flex-wrap gap-2" aria-label="Tệp âm thanh đã xử lý">
      {job.audio_assets.map((audio, index) => <span className="rounded bg-slate-50 px-2 py-1" key={index}>
        {audioLabels[audio.audio_type] ?? audio.audio_type}
        {' · '}{audio.sample_rate} Hz · {audio.channels} kênh
      </span>)}
    </div>}
    {!!job.dialects?.length && <ul className="mt-2 flex flex-wrap gap-2" aria-label="Vùng giọng dự đoán">
      {job.dialects.map(dialect => <li key={dialect.dialect_label} className="rounded bg-slate-50 px-2 py-1">
        {dialectLabels[dialect.dialect_label] ?? dialect.dialect_label} · {dialect.intervals} đoạn
        {dialect.duration_ms != null && ` · ${(Number(dialect.duration_ms) / 1000).toLocaleString('vi-VN', {maximumFractionDigits: 1})} giây`}
      </li>)}
    </ul>}
  </section>
}
