// Garaj Baras — i18n.jsx
//
// Lightweight bilingual (English / Hindi) layer. Rather than a key dictionary,
// strings are translated inline at their call site with `t('English', 'हिंदी')`
// so the source stays readable and nothing can drift out of sync with a
// separate messages file.
//
// Exports:
//   <LangProvider>       wrap the app (reads/writes gb_lang in localStorage)
//   useLang()            { lang, setLang, chosen }  — lang is 'en' | 'hi'
//   useT()               returns t(en, hi) bound to the current language
//   <LanguageChooser>    first-run modal: pick Hindi or English
//   <LangToggle>         compact EN / हिं switch for the nav bar
//
// A module-level `currentLang` mirror lets non-component helpers translate too
// (e.g. strings built inside plain functions); components still re-render via
// context when the language changes.

/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useState, useCallback, useMemo } from 'react'

const LANG_KEY = 'gb_lang'
const LangCtx = createContext(null)

// Module mirror of the active language for non-hook callers.
let currentLang = 'en'
try {
  const saved = localStorage.getItem(LANG_KEY)
  if (saved === 'hi' || saved === 'en') currentLang = saved
} catch { /* ignore */ }

// Standalone translator for plain (non-component) helper functions.
export function tr(en, hi) {
  return currentLang === 'hi' && hi != null ? hi : en
}

export function useLang() {
  return useContext(LangCtx)
}

export function useT() {
  const ctx = useContext(LangCtx)
  const lang = ctx?.lang || 'en'
  return useCallback((en, hi) => (lang === 'hi' && hi != null ? hi : en), [lang])
}

export function LangProvider({ children }) {
  const [lang, setLangState] = useState(() => {
    try {
      const saved = localStorage.getItem(LANG_KEY)
      if (saved === 'hi' || saved === 'en') return saved
    } catch { /* ignore */ }
    return null   // null = not chosen yet → first-run chooser shows
  })

  const setLang = useCallback((next) => {
    const val = next === 'hi' ? 'hi' : 'en'
    currentLang = val
    try { localStorage.setItem(LANG_KEY, val) } catch { /* ignore */ }
    try { document.documentElement.setAttribute('lang', val) } catch { /* ignore */ }
    try { document.documentElement.setAttribute('data-lang', val) } catch { /* ignore */ }
    setLangState(val)
  }, [])

  // Effective language for translating (before an explicit choice, default en).
  const effective = lang || 'en'
  currentLang = effective
  try { document.documentElement.setAttribute('data-lang', effective) } catch { /* ignore */ }

  const value = useMemo(() => ({
    lang: effective,
    setLang,
    chosen: lang !== null,
  }), [effective, setLang, lang])

  return (
    <LangCtx.Provider value={value}>
      {children}
      {lang === null && <LanguageChooser onPick={setLang} />}
    </LangCtx.Provider>
  )
}

// ── First-run language chooser ──────────────────────────────────────────────
export function LanguageChooser({ onPick }) {
  return (
    <div className="lang-overlay" role="dialog" aria-modal="true" aria-label="Choose language">
      <div className="lang-modal">
        <div className="lang-modal__brand">☔ Garaj Baras · गरज बरस</div>
        <div className="lang-modal__title">Choose your language</div>
        <div className="lang-modal__title-hi">अपनी भाषा चुनें</div>
        <div className="lang-modal__opts">
          <button type="button" className="lang-modal__opt" onClick={() => onPick('en')}>
            <span className="lang-modal__opt-big">English</span>
            <span className="lang-modal__opt-sub">Continue in English</span>
          </button>
          <button type="button" className="lang-modal__opt lang-modal__opt--hi" onClick={() => onPick('hi')}>
            <span className="lang-modal__opt-big">हिंदी</span>
            <span className="lang-modal__opt-sub">हिंदी में आगे बढ़ें</span>
          </button>
        </div>
        <div className="lang-modal__note">You can switch anytime · इसे कभी भी बदल सकते हैं</div>
      </div>
    </div>
  )
}

// ── Nav toggle: EN / हिं ────────────────────────────────────────────────────
export function LangToggle() {
  const ctx = useLang()
  if (!ctx) return null
  const { lang, setLang } = ctx
  return (
    <div className="lang-toggle" role="group" aria-label="Language">
      <button
        type="button"
        className={`lang-toggle__btn${lang === 'en' ? ' is-active' : ''}`}
        onClick={() => setLang('en')}
        aria-pressed={lang === 'en'}
      >EN</button>
      <button
        type="button"
        className={`lang-toggle__btn${lang === 'hi' ? ' is-active' : ''}`}
        onClick={() => setLang('hi')}
        aria-pressed={lang === 'hi'}
      >हिं</button>
    </div>
  )
}
