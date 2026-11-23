import { useState, type FormEvent } from 'react'
import { LogIn, X } from 'lucide-react'

export type Account = {user_id: string; username: string; email: string; role: 'USER'|'ADMIN'; status: 'ACTIVE'}

type Props = {api: string; onClose: () => void; onLogin: (account: Account) => void}

export default function Login({api, onClose, onLogin}: Props) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    const fields = new FormData(event.currentTarget)
    setPending(true)
    setError('')
    try {
      const response = await fetch(`${api}/auth/login`, {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({identifier: fields.get('identifier'), password: fields.get('password')}),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Không thể đăng nhập.')
      onLogin(result)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Không thể đăng nhập.')
    } finally {
      setPending(false)
    }
  }

  return <div className="modal-backdrop" onClick={() => !pending && onClose()}>
    <form className="modal" role="dialog" aria-modal="true" aria-labelledby="login-title"
      onClick={event => event.stopPropagation()} onSubmit={submit}>
      <div className="modal-title">
        <h2 id="login-title">Đăng nhập</h2>
        <button type="button" className="icon-button" aria-label="Đóng" disabled={pending} onClick={onClose}><X size={18}/></button>
      </div>
      <label htmlFor="login-identifier">Tên đăng nhập hoặc email</label>
      <input id="login-identifier" name="identifier" autoComplete="username" autoFocus required maxLength={255}/>
      <label htmlFor="login-password">Mật khẩu</label>
      <input id="login-password" name="password" type="password" autoComplete="current-password" required maxLength={128}/>
      {error && <p className="modal-error" role="alert">{error}</p>}
      <div className="modal-actions">
        <button type="button" className="secondary" disabled={pending} onClick={onClose}>Hủy</button>
        <button type="submit" className="primary" disabled={pending}><LogIn size={16}/>{pending ? 'Đang đăng nhập…' : 'Đăng nhập'}</button>
      </div>
    </form>
  </div>
}
