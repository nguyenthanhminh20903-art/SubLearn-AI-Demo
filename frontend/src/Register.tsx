import { useState, type FormEvent } from 'react'
import { UserPlus, X } from 'lucide-react'

type Props = {api: string; onClose: () => void; onRegistered: (username: string) => void}

export default function Register({api, onClose, onRegistered}: Props) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    const fields = new FormData(event.currentTarget)
    const password = fields.get('password')
    if (password !== fields.get('confirmation')) {
      setError('Hai mật khẩu chưa khớp.')
      return
    }
    setPending(true)
    setError('')
    try {
      const response = await fetch(`${api}/auth/register`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: fields.get('username'), email: fields.get('email'), password}),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Không thể đăng ký.')
      onRegistered(result.username)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Không thể đăng ký.')
    } finally {
      setPending(false)
    }
  }

  return <div className="modal-backdrop" onClick={() => !pending && onClose()}>
    <form className="modal" role="dialog" aria-modal="true" aria-labelledby="register-title"
      onClick={event => event.stopPropagation()} onSubmit={submit}>
      <div className="modal-title">
        <h2 id="register-title">Đăng ký tài khoản</h2>
        <button type="button" className="icon-button" aria-label="Đóng" disabled={pending} onClick={onClose}><X size={18}/></button>
      </div>
      <label htmlFor="register-username">Tên đăng nhập</label>
      <input id="register-username" name="username" autoComplete="username" autoFocus required minLength={3} maxLength={100}/>
      <label htmlFor="register-email">Email</label>
      <input id="register-email" name="email" type="email" autoComplete="email" required maxLength={255}/>
      <label htmlFor="register-password">Mật khẩu (ít nhất 12 ký tự)</label>
      <input id="register-password" name="password" type="password" autoComplete="new-password" required minLength={12} maxLength={128}/>
      <label htmlFor="register-confirmation">Nhập lại mật khẩu</label>
      <input id="register-confirmation" name="confirmation" type="password" autoComplete="new-password" required maxLength={128}/>
      {error && <p className="modal-error" role="alert">{error}</p>}
      <div className="modal-actions">
        <button type="button" className="secondary" onClick={onClose} disabled={pending}>Hủy</button>
        <button type="submit" className="primary" disabled={pending}><UserPlus size={16}/>{pending ? 'Đang đăng ký…' : 'Đăng ký'}</button>
      </div>
    </form>
  </div>
}
