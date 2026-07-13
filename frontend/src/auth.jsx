// Garaj Baras — auth.jsx
//
// Auth context + login UI on top of Supabase Auth (Google OAuth + email OTP).
// Policy: Route + Nowcast are fully free; everything else (Ask AI, rain
// alerts, saved places) requires sign-in — gate with <SignInGate>.
//
// Exports:
//   <AuthProvider>   wrap the app
//   useAuth()        { user, session, ready, configured, openLogin, signOut, authHeaders }
//   <AccountButton>  compact sign-in / avatar button for the nav bar
//   <SignInGate>     renders children when signed in, else a sign-in prompt card
//   <LoginModal>     rendered by AuthProvider; opened via openLogin()

/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { supabase, authConfigured } from './supabase'
import { useT } from './i18n'

const AuthCtx = createContext(null)

export function useAuth() {
  return useContext(AuthCtx)
}

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null)
  const [ready, setReady] = useState(!authConfigured)
  const [loginOpen, setLoginOpen] = useState(false)

  useEffect(() => {
    if (!supabase) return
    supabase.auth.getSession().then(({ data }) => {
      setSession(data?.session || null)
      setReady(true)
    })
    const { data: sub } = supabase.auth.onAuthStateChange((_evt, s) => {
      setSession(s || null)
      if (s) setLoginOpen(false)
    })
    return () => sub?.subscription?.unsubscribe()
  }, [])

  const signOut = useCallback(async () => {
    try { await supabase?.auth.signOut() } catch { /* ignore */ }
  }, [])

  // Axios/fetch headers for backend calls that need the user.
  const authHeaders = useCallback(() => (
    session?.access_token ? { Authorization: `Bearer ${session.access_token}` } : {}
  ), [session])

  const value = {
    user: session?.user || null,
    session,
    ready,
    configured: authConfigured,
    openLogin: () => setLoginOpen(true),
    signOut,
    authHeaders,
  }

  return (
    <AuthCtx.Provider value={value}>
      {children}
      {loginOpen && <LoginModal onClose={() => setLoginOpen(false)} />}
    </AuthCtx.Provider>
  )
}

// ── Nav button: "Sign in" or the signed-in avatar/menu ─────────────────────
export function AccountButton() {
  const t = useT()
  const { user, configured, openLogin, signOut } = useAuth()
  const [menuOpen, setMenuOpen] = useState(false)
  if (!configured) return null

  if (!user) {
    return (
      <button type="button" className="acct-btn" onClick={openLogin}>
        {t('Sign in', 'साइन इन')}
      </button>
    )
  }
  const label = user.email ? user.email[0].toUpperCase() : '•'
  return (
    <div className="acct-wrap">
      <button
        type="button"
        className="acct-btn acct-btn--avatar"
        title={user.email || t('Account', 'खाता')}
        onClick={() => setMenuOpen((v) => !v)}
        onBlur={() => setTimeout(() => setMenuOpen(false), 140)}
      >
        {label}
      </button>
      {menuOpen && (
        <div className="acct-menu">
          <div className="acct-menu__email">{user.email}</div>
          <button type="button" className="acct-menu__item" onMouseDown={(e) => e.preventDefault()}
            onClick={() => { setMenuOpen(false); signOut() }}>
            {t('Sign out', 'साइन आउट')}
          </button>
        </div>
      )}
    </div>
  )
}

// ── Gate: children only when signed in ──────────────────────────────────────
export function SignInGate({ children, title, sub }) {
  const t = useT()
  const { user, ready, configured, openLogin } = useAuth()
  if (user) return children
  return (
    <div className="gate-card">
      <div className="gate-card__icon" aria-hidden>🔒</div>
      <div className="gate-card__title">{title || t('Sign in to continue', 'जारी रखने के लिए साइन इन करें')}</div>
      <div className="gate-card__sub">
        {sub || t('This feature is tied to your account. Route check and nowcast stay free — no login needed there.', 'यह सुविधा आपके खाते से जुड़ी है। रास्ता जाँच और तात्कालिक पूर्वानुमान मुफ़्त हैं — वहाँ लॉगिन की ज़रूरत नहीं।')}
      </div>
      {configured ? (
        <button type="button" className="gate-card__btn" disabled={!ready} onClick={openLogin}>
          {t('Sign in / Sign up', 'साइन इन / साइन अप')}
        </button>
      ) : (
        <div className="gate-card__sub" style={{ opacity: 0.7 }}>
          {t('Sign-in isn’t configured on this deployment yet (set VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY).', 'इस डिप्लॉयमेंट पर साइन-इन अभी कॉन्फ़िगर नहीं है (VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY सेट करें)।')}
        </div>
      )}
    </div>
  )
}

// ── Login modal: Google + email OTP (6-digit code) ──────────────────────────
export function LoginModal({ onClose }) {
  const t = useT()
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [stage, setStage] = useState('start')   // start | code | working
  const [error, setError] = useState(null)

  async function google() {
    setError(null)
    try {
      const { error: err } = await supabase.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin },
      })
      if (err) setError(err.message)
      // On success the browser redirects to Google; nothing else to do here.
    } catch (e) {
      setError(String(e?.message || e))
    }
  }

  async function sendOtp() {
    const em = email.trim()
    if (!/^\S+@\S+\.\S+$/.test(em)) { setError(t('Enter a valid email address.', 'एक वैध ईमेल पता दर्ज करें।')); return }
    setError(null); setStage('working')
    const { error: err } = await supabase.auth.signInWithOtp({
      email: em,
      options: { shouldCreateUser: true },
    })
    if (err) { setError(err.message); setStage('start') }
    else setStage('code')
  }

  async function verifyOtp() {
    const token = code.trim()
    if (token.length < 6) { setError(t('Enter the 6-digit code from your email.', 'अपने ईमेल से 6-अंकों का कोड दर्ज करें।')); return }
    setError(null); setStage('working')
    const { error: err } = await supabase.auth.verifyOtp({
      email: email.trim(), token, type: 'email',
    })
    if (err) { setError(err.message); setStage('code') }
    // Success closes the modal via onAuthStateChange in AuthProvider.
  }

  return (
    <div className="login-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="login-modal" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="login-modal__close" aria-label={t('Close', 'बंद करें')} onClick={onClose}>×</button>
        <div className="login-modal__title">{t('Sign in to Garaj Baras', 'गरज बरस में साइन इन करें')}</div>
        <div className="login-modal__sub">
          {t('Saved places, rain alerts and the AI assistant live on your account. Route check & nowcast are always free.', 'सहेजे गए स्थान, बारिश अलर्ट और AI सहायक आपके खाते पर रहते हैं। रास्ता जाँच और तात्कालिक पूर्वानुमान हमेशा मुफ़्त हैं।')}
        </div>

        {stage !== 'code' && (
          <>
            <button type="button" className="login-modal__google" onClick={google} disabled={stage === 'working'}>
              <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden>
                <path fill="#FFC107" d="M43.6 20H24v8h11.3C33.7 33.4 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3 0 5.8 1.1 7.9 3l5.7-5.7C34 5.8 29.3 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.7-.4-4z"/>
                <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15 19 12 24 12c3 0 5.8 1.1 7.9 3l5.7-5.7C34 5.8 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>
                <path fill="#4CAF50" d="M24 44c5.2 0 9.9-1.7 13.4-4.7l-6.2-5.2C29.2 35.4 26.7 36 24 36c-5.3 0-9.7-2.6-11.3-7l-6.5 5C9.5 39.6 16.2 44 24 44z"/>
                <path fill="#1976D2" d="M43.6 20H24v8h11.3c-.8 2.3-2.3 4.3-4.1 5.7l6.2 5.2C41.4 35.4 44 30.1 44 24c0-1.3-.1-2.7-.4-4z"/>
              </svg>
              {t('Continue with Google', 'Google से जारी रखें')}
            </button>
            <div className="login-modal__or">{t('or', 'या')}</div>
            <input
              className="login-modal__input"
              type="email"
              placeholder="you@email.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && sendOtp()}
            />
            <button type="button" className="login-modal__primary" onClick={sendOtp} disabled={stage === 'working'}>
              {stage === 'working' ? t('Sending…', 'भेजा जा रहा है…') : t('Email me a code', 'मुझे ईमेल पर कोड भेजें')}
            </button>
          </>
        )}

        {stage === 'code' && (
          <>
            <div className="login-modal__sent">{t('Code sent to', 'कोड भेजा गया')} <b>{email.trim()}</b></div>
            <input
              className="login-modal__input login-modal__input--code"
              inputMode="numeric"
              maxLength={8}
              placeholder={t('6-digit code', '6-अंकों का कोड')}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && verifyOtp()}
              autoFocus
            />
            <button type="button" className="login-modal__primary" onClick={verifyOtp}>
              {t('Verify & sign in', 'सत्यापित करें और साइन इन करें')}
            </button>
            <button type="button" className="login-modal__link" onClick={() => { setStage('start'); setCode('') }}>
              {t('Use a different email', 'दूसरा ईमेल उपयोग करें')}
            </button>
          </>
        )}

        {error && <div className="login-modal__error">{error}</div>}
      </div>
    </div>
  )
}
