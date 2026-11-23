import {useEffect, useState, type FormEvent} from 'react'
import {ArrowDownToLine, X} from 'lucide-react'
import {exportVideoRequest, startVideoDownload} from './videoDownload'

type Mode = 'original' | 'translation' | 'bilingual'
type Cue = {segment_id: string; start_ms: number; end_ms: number; content: string; source_segment_ids: string[]}
type Props = {
  api: string
  exportPath: string
  mediaReady: boolean
  originalId: string
  translationId?: string
  onClose: () => void
}

async function getJson(api: string, path: string, options?: RequestInit) {
  const response = await fetch(`${api}${path}`, {credentials: 'include', ...options})
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Không tải được phiên bản phụ đề.')
  return data
}

export default function RevisionExport({api, exportPath, mediaReady, originalId, translationId, onClose}: Props) {
  const [mode, setMode] = useState<Mode>('original')
  const [sourceVersions, setSourceVersions] = useState<number[]>([])
  const [targetVersions, setTargetVersions] = useState<number[]>([])
  const [source, setSource] = useState(0)
  const [target, setTarget] = useState(0)
  const [preview, setPreview] = useState<Cue[] | null>(null)
  const [error, setError] = useState('')
  const [exporting, setExporting] = useState(false)
  const [downloadUrl, setDownloadUrl] = useState('')
  const track = mode === 'original' ? originalId : translationId
  const version = mode === 'original' ? source : target

  useEffect(() => {
    const controller = new AbortController()
    const options = {signal: controller.signal}
    Promise.all([
      getJson(api, `/tracks/${originalId}`, options),
      translationId ? getJson(api, `/tracks/${translationId}`, options) : null,
    ]).then(([original, translation]) => {
      if (controller.signal.aborted) return
      setSourceVersions(original.versions)
      setSource(original.versions[0] ?? 0)
      setTargetVersions(translation?.versions ?? [])
      setTarget(translation?.versions[0] ?? 0)
    }).catch(failure => {if (!controller.signal.aborted) setError(failure.message)})
    return () => controller.abort()
  }, [api, originalId, translationId])

  useEffect(() => {
    setDownloadUrl('')
    if (!track || !version || (mode === 'bilingual' && !source)) return
    const controller = new AbortController()
    const options = {signal: controller.signal}
    Promise.all([
      getJson(api, `/tracks/${track}/revisions/${version}`, options),
      mode === 'bilingual' ? getJson(api, `/tracks/${originalId}/revisions/${source}`, options) : null,
    ]).then(([selected, original]) => {
      if (controller.signal.aborted) return
      if (!original) {
        setPreview(selected.segments)
        return
      }
      const sourceCues: Cue[] = original.segments
      const byId = new Map(sourceCues.map(cue => [cue.segment_id, cue.content]))
      const byTime = new Map(sourceCues.map(cue => [`${cue.start_ms}:${cue.end_ms}`, cue.content]))
      setPreview(selected.segments.map((cue: Cue) => {
        const text = cue.source_segment_ids.map(id => byId.get(id)).filter(Boolean).join(' ') ||
          byTime.get(`${cue.start_ms}:${cue.end_ms}`)
        return {...cue, content: text ? `${text}\n${cue.content}` : cue.content}
      }))
    })
      .catch(failure => {if (!controller.signal.aborted) setError(failure.message)})
    return () => controller.abort()
  }, [api, mode, originalId, source, track, version])

  async function download(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!mediaReady || !preview?.length || exporting) return
    setError('')
    setDownloadUrl('')
    setExporting(true)
    try {
      const cues = preview.map(({start_ms, end_ms, content}) => ({start_ms, end_ms, text: content}))
      const result = await getJson(api, exportPath, exportVideoRequest(cues))
      const url = `${api}${result.download_url}`
      setDownloadUrl(url)
      startVideoDownload(url)
    } catch (failure) {
      setError((failure as Error).message)
    } finally {
      setExporting(false)
    }
  }

  return <div className="modal-backdrop" onClick={() => !exporting && onClose()}>
    <form className="modal" role="dialog" aria-modal="true" aria-labelledby="revision-title"
      onClick={event => event.stopPropagation()} onSubmit={download}>
      <div className="modal-title">
        <h2 id="revision-title">Phiên bản phụ đề</h2>
        <button type="button" className="icon-button" aria-label="Đóng" disabled={exporting} onClick={onClose}><X size={18}/></button>
      </div>
      <div className="subtitle-options">
        <label>Track<select autoFocus value={mode} disabled={exporting} onChange={event => {
          setPreview(null)
          setError('')
          setMode(event.target.value as Mode)
        }}>
          <option value="original">Bản gốc</option>
          <option value="translation" disabled={!targetVersions.length}>Bản dịch</option>
          <option value="bilingual" disabled={!targetVersions.length}>Song ngữ</option>
        </select></label>
        {mode !== 'translation' && <label>Phiên bản gốc<select value={source} disabled={exporting}
          onChange={event => {setPreview(null); setSource(Number(event.target.value))}}>
          {sourceVersions.map(value => <option key={value} value={value}>Phiên bản {value}</option>)}
        </select></label>}
        {mode !== 'original' && <label>Phiên bản dịch<select value={target} disabled={exporting}
          onChange={event => {setPreview(null); setTarget(Number(event.target.value))}}>
          {targetVersions.map(value => <option key={value} value={value}>Phiên bản {value}</option>)}
        </select></label>}
      </div>
      {preview && <div className="mt-3 max-h-56 overflow-y-auto rounded-lg bg-slate-50 p-3 text-sm">
        {preview.length ? preview.map(cue => <p key={cue.segment_id} className="mb-2 whitespace-pre-line">
          <span className="mr-2 text-slate-500">{(cue.start_ms / 1000).toFixed(2)} s</span>{cue.content}
        </p>) : <p>Phiên bản này không có đoạn phụ đề.</p>}
      </div>}
      {error && <p className="modal-error" role="alert">{error}</p>}
      <div className="modal-actions">
        {downloadUrl && <a className="secondary" href={downloadUrl} download>Tải lại MP4</a>}
        <button type="submit" className="primary" disabled={exporting || !mediaReady || !preview?.length}>
          <ArrowDownToLine size={16}/>{exporting ? 'Đang tạo video…' : 'Tải video phiên bản này'}
        </button>
      </div>
    </form>
  </div>
}
