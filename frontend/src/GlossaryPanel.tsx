import {useEffect, useState, type FormEvent} from 'react'
import {X} from 'lucide-react'

type Language = 'vi' | 'en'
type Term = {id: string; source_term: string; translated_term: string}
type Props = {api: string; source: Language; target: Language; onClose: () => void}
const names = {vi: 'Tiếng Việt', en: 'Tiếng Anh'}

async function glossaryRequest(api: string, path: string, options?: RequestInit) {
  const response = await fetch(`${api}/glossary${path}`, {credentials: 'include', ...options})
  if (response.status === 204) return null
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Không cập nhật được thuật ngữ.')
  return data
}

export default function GlossaryPanel({api, source, target, onClose}: Props) {
  const [terms, setTerms] = useState<Term[]>([])
  const [sourceTerm, setSourceTerm] = useState('')
  const [targetTerm, setTargetTerm] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const query = `?source=${source}&target=${target}`
  useEffect(() => {
    const controller = new AbortController()
    glossaryRequest(api, query, {signal: controller.signal}).then(setTerms)
      .catch(failure => {if (!controller.signal.aborted) setError(failure.message)})
    return () => controller.abort()
  }, [api, query])

  async function change(removeId?: string) {
    setPending(true)
    setError('')
    try {
      if (removeId) await glossaryRequest(api, `/${removeId}`, {method: 'DELETE'})
      else await glossaryRequest(api, '', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          source_language: source, target_language: target, source_term: sourceTerm, translated_term: targetTerm,
        }),
      })
      setTerms(await glossaryRequest(api, query))
      setSourceTerm('')
      setTargetTerm('')
    } catch (failure) {
      setError((failure as Error).message)
    } finally {
      setPending(false)
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!pending) void change()
  }

  return <div className="modal-backdrop" onClick={() => !pending && onClose()}>
    <form className="modal" role="dialog" aria-modal="true" aria-labelledby="glossary-title"
      onClick={event => event.stopPropagation()} onSubmit={submit}>
      <div className="modal-title">
        <h2 id="glossary-title">Thuật ngữ dịch</h2>
        <button type="button" className="icon-button" aria-label="Đóng" disabled={pending} onClick={onClose}><X size={18}/></button>
      </div>
      <label htmlFor="source-term">{names[source]}</label>
      <input id="source-term" value={sourceTerm} onChange={event => setSourceTerm(event.target.value)}
        autoFocus required maxLength={255} disabled={pending}/>
      <label htmlFor="target-term">{names[target]}</label>
      <input id="target-term" value={targetTerm} onChange={event => setTargetTerm(event.target.value)}
        required maxLength={255} disabled={pending}/>
      {error && <p className="modal-error" role="alert">{error}</p>}
      <div className="modal-actions">
        <button type="submit" className="primary" disabled={pending}>Lưu thuật ngữ</button>
      </div>
      <ul className="mt-3 max-h-56 overflow-y-auto">
        {terms.map(term => <li key={term.id} className="mb-2 flex items-center justify-between gap-2">
          <button type="button" className="text-btn text-left" disabled={pending} onClick={() => {
            setSourceTerm(term.source_term); setTargetTerm(term.translated_term)
          }}>{term.source_term} → {term.translated_term}</button>
          <button type="button" className="secondary small" disabled={pending}
            aria-label={`Xóa thuật ngữ ${term.source_term}`} onClick={() => void change(term.id)}>Xóa</button>
        </li>)}
      </ul>
    </form>
  </div>
}
