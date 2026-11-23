/// <reference types="vite/client" />
import {useEffect, useRef, useState, type FormEvent} from 'react'
import {AudioLines, Mic} from 'lucide-react'

type Language = 'vi' | 'en'
type State = 'idle' | 'starting' | 'recording' | 'stopping' | 'finished'
type Cue = {id: string; start_ms: number; end_ms: number; text: string; translation: string}
type LiveEvent =
  | {type: 'ready' | 'ack'}
  | {type: 'partial'; text: string}
  | {type: 'final'; segments: Cue[]; latency_ms: number; warning: string | null}
  | {type: 'finished'; project_id: string; status: string}
  | {type: 'error'; message: string}
type Runtime = {
  context?: AudioContext
  mic?: MediaStream
  capture?: AudioWorkletNode
  socket: WebSocket
  id: string
  queue: ArrayBuffer[]
  ready: boolean
  waiting: boolean
  stopping: boolean
  flushed: boolean
}
type Props = {
  api: string
  projects: {id: string; name: string}[]
  signedIn: boolean
  contextAvailable: boolean
  onLogin: () => void
  onOpenProject: (id: string) => void
  onProjectsChanged: () => void
}

function closeCapture(item: Runtime) {
  item.mic?.getTracks().forEach(track => track.stop())
  if (item.context && item.context.state !== 'closed') void item.context.close().catch(() => {})
}

function sendNext(item: Runtime) {
  if (!item.ready || item.waiting || item.socket.readyState !== WebSocket.OPEN) return
  const frame = item.queue.shift()
  if (frame) {
    item.waiting = true
    item.socket.send(frame)
  } else if (item.stopping && item.flushed) {
    item.waiting = true
    item.socket.send('stop')
  }
}

function closeUnstarted(api: string, id: string) {
  return fetch(`${api}/live/sessions/${id}/stop`, {
    method: 'POST', credentials: 'include', keepalive: true,
  }).catch(() => {})
}

export default function LivePanel({api, projects, signedIn, contextAvailable, onLogin, onOpenProject, onProjectsChanged}: Props) {
  const [state, setState] = useState<State>('idle')
  const [language, setLanguage] = useState<Language>('vi')
  const [source, setSource] = useState<'MICROPHONE' | 'STREAM_URL'>('MICROPHONE')
  const [translate, setTranslate] = useState(false)
  const [translationMode, setTranslationMode] = useState<'local' | 'context'>('local')
  const [partial, setPartial] = useState('')
  const [cues, setCues] = useState<Cue[]>([])
  const [latency, setLatency] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [finishedProject, setFinishedProject] = useState('')
  const runtime = useRef<Runtime | null>(null)
  const generation = useRef(0)
  const busy = state === 'starting' || state === 'recording' || state === 'stopping'

  useEffect(() => () => {
    generation.current++
    const active = runtime.current
    if (active) {
      runtime.current = null
      closeCapture(active)
      active.socket.close()
      if (!active.ready) void closeUnstarted(api, active.id)
    }
  }, [api])

  function stop(item: Runtime) {
    if (item.stopping) return
    item.stopping = true
    setState('stopping')
    if (item.capture) item.capture.port.postMessage('flush')
    else {
      item.flushed = true
      sendNext(item)
    }
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    if (!signedIn) {onLogin(); return}
    const fields = new FormData(event.currentTarget)
    const instance = ++generation.current
    let context: AudioContext | undefined
    let mic: MediaStream | undefined
    let capture: AudioWorkletNode | undefined
    let sessionId: string | undefined
    setState('starting')
    setError('')
    setPartial('')
    setCues([])
    setLatency(null)
    setFinishedProject('')
    try {
      if (source === 'MICROPHONE') {
        if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) {
          throw new Error('Trình duyệt cần hỗ trợ microphone và AudioWorklet trên localhost hoặc HTTPS.')
        }
        context = new AudioContext({sampleRate: 16000})
        if (!context.audioWorklet || context.sampleRate !== 16000) {
          throw new Error('Trình duyệt chưa hỗ trợ thu âm PCM 16 kHz.')
        }
        await context.resume()
        mic = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true}})
        if (generation.current !== instance) return
        await context.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-capture.js`)
        capture = new AudioWorkletNode(context, 'pcm-capture', {channelCount: 1})
        context.createMediaStreamSource(mic).connect(capture).connect(context.destination)
      }
      if (generation.current !== instance) return
      const response = await fetch(`${api}/live/sessions`, {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          title: String(fields.get('title')).trim(),
          project_id: fields.get('project') || null,
          source_type: source,
          stream_url: source === 'STREAM_URL' ? String(fields.get('stream')).trim() : null,
          source_language: language,
          target_language: translate ? (language === 'vi' ? 'en' : 'vi') : null,
          translation_mode: translate ? translationMode : 'local',
          chunk_size_ms: Number(fields.get('chunk')),
          overlap_ms: Number(fields.get('overlap')),
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Cấu hình phiên Live không hợp lệ.')
      sessionId = String(data.session_id)
      if (generation.current !== instance) return
      const endpoint = new URL(data.websocket_path, api)
      endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new WebSocket(endpoint)
      const item: Runtime = {
        context, mic, capture, socket, id: sessionId, queue: [], ready: false,
        waiting: false, stopping: false, flushed: false,
      }
      runtime.current = item
      if (capture) capture.port.onmessage = ({data: frame}: MessageEvent<ArrayBuffer | string>) => {
        if (runtime.current !== item) return
        if (frame === 'flushed') {
          item.flushed = true
          closeCapture(item)
        } else if (frame instanceof ArrayBuffer) {
          item.queue.push(frame)
          if (item.queue.length >= 120 && !item.stopping) {
            setError('Máy chủ xử lý chậm hơn tốc độ thu. Phiên đang lưu phần âm thanh đã nhận.')
            stop(item)
          }
        }
        sendNext(item)
      }
      socket.onmessage = event => {
        if (runtime.current !== item) return
        const message: LiveEvent = JSON.parse(event.data)
        if (message.type === 'ready') {
          item.ready = true
          capture?.port.postMessage('start')
          setState('recording')
        } else if (message.type === 'ack') {
          item.waiting = false
          sendNext(item)
        } else if (message.type === 'partial') {
          setPartial(message.text)
        } else if (message.type === 'final') {
          setPartial('')
          setLatency(message.latency_ms)
          setCues(previous => [...previous, ...message.segments])
          if (message.warning) setError(message.warning)
        } else if (message.type === 'finished') {
          runtime.current = null
          closeCapture(item)
          setState('finished')
          setFinishedProject(message.project_id)
          onProjectsChanged()
        } else if (message.type === 'error') {
          setError(message.message)
        }
      }
      socket.onclose = () => {
        if (runtime.current !== item) return
        runtime.current = null
        closeCapture(item)
        if (!item.ready) void closeUnstarted(api, item.id)
        setPartial('')
        setState('idle')
        setError(previous => previous || 'Kết nối Live đã ngắt. Các đoạn đã chốt có trong dự án.')
        onProjectsChanged()
      }
    } catch (failure) {
      if (generation.current === instance) {
        setError((failure as Error).name === 'NotAllowedError' ? 'Bạn cần cấp quyền microphone để bắt đầu.' :
          (failure as Error).message)
        setState('idle')
      }
    } finally {
      if (!runtime.current || runtime.current.context !== context) {
        mic?.getTracks().forEach(track => track.stop())
        if (context && context.state !== 'closed') void context.close().catch(() => {})
        if (sessionId) void closeUnstarted(api, sessionId)
      }
    }
  }

  return <div className="content">
    <div className="page-heading"><div>
      <h1>Phụ đề Live</h1>
      <p>Nhận dạng âm thanh trực tiếp qua máy chủ và lưu các đoạn đã chốt vào dự án.</p>
    </div></div>
    <div className="live-layout">
      <form className="live-control control-form" onSubmit={start}>
        <div className="mic-orb"><div><Mic size={32}/></div></div>
        <h2>{state === 'recording' ? 'Đang nhận âm thanh' : state === 'stopping' ? 'Đang lưu phiên' : 'Phiên Live'}</h2>
        {signedIn && <fieldset disabled={busy} className="grid gap-2 text-left">
          <label className="grid">Tên phiên<input name="title" required maxLength={255} defaultValue="Phụ đề Live"/></label>
          <label className="grid">Dự án<select name="project" defaultValue="">
            <option value="">Tạo dự án mới</option>
            {projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select></label>
          <label className="grid">Nguồn âm thanh<select value={source} onChange={event => setSource(event.target.value as typeof source)}>
            <option value="MICROPHONE">Microphone</option><option value="STREAM_URL">URL luồng trực tiếp</option>
          </select></label>
          {source === 'STREAM_URL' && <label className="grid">URL HTTPS đã cấu hình
            <input name="stream" type="url" required maxLength={2083}/>
          </label>}
          <label className="grid">Ngôn ngữ gốc<select value={language} onChange={event => setLanguage(event.target.value as Language)}>
            <option value="vi">Tiếng Việt</option><option value="en">Tiếng Anh</option>
          </select></label>
          <label className="flex gap-2"><input type="checkbox" checked={translate} onChange={event => setTranslate(event.target.checked)}/>
            Dịch đoạn đã chốt sang {language === 'vi' ? 'tiếng Anh' : 'tiếng Việt'}
          </label>
          {translate && contextAvailable && <label className="grid">Chế độ dịch<select value={translationMode}
            onChange={event => setTranslationMode(event.target.value as typeof translationMode)}>
            <option value="local">Dịch trên máy</option><option value="context">Dịch theo ngữ cảnh</option>
          </select></label>}
          <div className="grid grid-cols-2 gap-2">
            <label className="grid">Kích thước đoạn (ms)<input type="number" name="chunk" defaultValue={6000}
              min={2000} max={10000} step={500} required/></label>
            <label className="grid">Chồng lấn (ms)<input type="number" name="overlap" defaultValue={1000}
              min={0} max={2000} step={500} required/></label>
          </div>
        </fieldset>}
        {error && <p className="modal-error" role="alert">{error}</p>}
        {!signedIn ? <button type="button" className="primary wide" onClick={onLogin}>Đăng nhập để bắt đầu</button> :
          state === 'recording' ? <button type="button" className="stop-btn wide" onClick={() => {
            if (runtime.current) stop(runtime.current)
          }}>Kết thúc và lưu</button> :
          <button type="submit" className="primary wide" disabled={busy}>
            <Mic size={17}/>{state === 'starting' ? 'Đang kết nối…' : state === 'stopping' ? 'Đang xử lý phần còn lại…' : 'Bắt đầu Live'}
          </button>}
      </form>
      <section className="live-transcript" aria-label="Phụ đề trực tiếp">
        <div className="panel-head"><div><b>Bản ghi trực tiếp</b><small>{cues.length} đoạn đã chốt</small></div>
          {latency !== null && <span>Độ trễ xử lý {latency} ms</span>}
        </div>
        <div className="transcript-body" aria-live="polite">
          {!cues.length && !partial && <div className="transcript-empty"><AudioLines size={32}/><b>Chưa có phụ đề</b></div>}
          {cues.map(cue => <div className="transcript-line" key={cue.id}>
            <small>{(cue.start_ms / 1000).toFixed(2)} s</small>
            <span>{cue.text}{cue.translation && <><br/><em>{cue.translation}</em></>}</span>
          </div>)}
          {partial && <p className="transcript-line interim">{partial}</p>}
        </div>
        {finishedProject && <div className="transcript-footer">
          <button type="button" className="secondary" onClick={() => onOpenProject(finishedProject)}>Mở dự án đã lưu</button>
        </div>}
      </section>
    </div>
  </div>
}
