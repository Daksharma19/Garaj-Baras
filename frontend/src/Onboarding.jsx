import { useState } from 'react'
import { useT } from './i18n'

// First-run onboarding: a 3-card stepped intro explaining the app's core
// idea (radar nowcasting ≠ generic weather forecast) and the three main
// features. Shown once on first visit (gb_onboarded in localStorage) and
// re-openable via the "How it works" link on the planner screen.

export const ONBOARDING_KEY = 'gb_onboarded'

const ICONS = [
  (
    <svg viewBox="0 0 48 48" fill="none" width="52" height="52" aria-hidden>
      <path d="M10 40c0-14 8-18 14-24s4-10 4-10" stroke="#38BDF8" strokeWidth="3" strokeLinecap="round" strokeDasharray="0.1 7" />
      <circle cx="10" cy="40" r="4" fill="#22C55E" />
      <circle cx="30" cy="7" r="4" fill="#EF4444" />
      <path d="M34 26c3 0 5 2 5 4.5S37 35 34.5 35H26c-2.5 0-4.5-2-4.5-4.5 0-2.2 1.6-4 3.7-4.4A6 6 0 0 1 34 26z" fill="#0EA5E9" opacity="0.85" />
      <path d="M27 37.5l-1.5 3M31 37.5l-1.5 3" stroke="#7DD3FC" strokeWidth="2" strokeLinecap="round" />
    </svg>
  ),
  (
    <svg viewBox="0 0 48 48" fill="none" width="52" height="52" aria-hidden>
      <circle cx="24" cy="24" r="19" stroke="#38BDF8" strokeWidth="2" opacity="0.35" />
      <circle cx="24" cy="24" r="12" stroke="#38BDF8" strokeWidth="2" opacity="0.55" />
      <circle cx="24" cy="24" r="5" stroke="#38BDF8" strokeWidth="2" />
      <path d="M24 24L37 13" stroke="#38BDF8" strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="31" cy="30" r="2.5" fill="#F59E0B" />
      <circle cx="15" cy="18" r="2" fill="#0EA5E9" />
    </svg>
  ),
  (
    <svg viewBox="0 0 48 48" fill="none" width="52" height="52" aria-hidden>
      <path d="M24 6c-6 0-10 4.5-10 10.5V25l-4 6v2h28v-2l-4-6v-8.5C34 10.5 30 6 24 6z" stroke="#F59E0B" strokeWidth="2.5" strokeLinejoin="round" />
      <path d="M20 37a4 4 0 0 0 8 0" stroke="#F59E0B" strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="35" cy="10" r="5" fill="#EF4444" />
    </svg>
  ),
]

export default function Onboarding({ onClose }) {
  const t = useT()
  const [step, setStep] = useState(0)

  const STEPS = [
    {
      icon: ICONS[0],
      title: t('Scan your route', 'अपना रास्ता स्कैन करें'),
      body: (
        <>
          {t("Enter where you're going and the route gets colored by the rain you'll actually meet — ", 'बताएँ कि आप कहाँ जा रहे हैं और रास्ता उस बारिश के अनुसार रंगीन हो जाता है जो आपको वास्तव में मिलेगी — ')}
          <strong>{t("at each stretch, at the time you'll be there", 'हर हिस्से पर, उस समय जब आप वहाँ होंगे')}</strong>.
        </>
      ),
    },
    {
      icon: ICONS[1],
      title: t('Nowcast any location', 'किसी भी स्थान का तात्कालिक पूर्वानुमान'),
      body: (
        <>
          {t('The ', '')}<strong>{t('Nowcast', 'तात्कालिक पूर्वानुमान')}</strong>{t(' tab answers "will it rain here?" for the next ', ' टैब बताता है "क्या यहाँ बारिश होगी?" अगले ')}
          <strong>{t('~2 hours', '~2 घंटे')}</strong>{t(', in 15-minute steps, with an animated radar of where the rain was and where it\'s headed. Radar nowcasting is sharp inside that window — beyond ~2 hours we won\'t pretend to know.', ' के लिए, 15-मिनट के अंतराल में, एक एनिमेटेड रडार के साथ जो दिखाता है बारिश कहाँ थी और कहाँ जा रही है। इस अवधि के भीतर रडार तात्कालिक पूर्वानुमान सटीक है — ~2 घंटे के बाद हम जानने का दावा नहीं करेंगे।')}
        </>
      ),
    },
    {
      icon: ICONS[2],
      title: t('Alerts & live journeys', 'अलर्ट और लाइव सफ़र'),
      body: (
        <>
          {t('Sign in to get a ', 'साइन इन करें ताकि ')}<strong>{t('push notification', 'पुश सूचना')}</strong>{t(' when rain heads for your saved places. On the road, ', ' मिले जब बारिश आपके सहेजे गए स्थानों की ओर बढ़े। रास्ते में, ')}<strong>{t('Start Live Journey', 'लाइव सफ़र शुरू करें')}</strong>{' '}
          {t("tracks your GPS and warns you about rain ahead — even while your screen is off.", 'आपके GPS को ट्रैक करता है और आगे की बारिश के बारे में चेतावनी देता है — तब भी जब आपकी स्क्रीन बंद हो।')}
        </>
      ),
    },
  ]

  const last = step === STEPS.length - 1
  const s = STEPS[step]

  const finish = () => {
    try { localStorage.setItem(ONBOARDING_KEY, '1') } catch {}
    onClose()
  }

  return (
    <div className="onb-overlay" role="dialog" aria-modal="true" aria-labelledby="onb-title">
      <div className="onb-card">
        <button type="button" className="onb-skip" onClick={finish}>
          {t('Skip', 'छोड़ें')}
        </button>

        <div className="onb-icon" aria-hidden>{s.icon}</div>
        <h2 className="onb-title" id="onb-title">{s.title}</h2>
        <p className="onb-body">{s.body}</p>

        <div className="onb-dots" aria-hidden>
          {STEPS.map((_, i) => (
            <button
              key={i}
              type="button"
              className={`onb-dot${i === step ? ' onb-dot--active' : ''}`}
              onClick={() => setStep(i)}
              tabIndex={-1}
            />
          ))}
        </div>

        <button
          type="button"
          className="onb-next"
          onClick={() => (last ? finish() : setStep(step + 1))}
        >
          {last ? t("Let's go", 'चलिए शुरू करें') : t('Next', 'आगे')}
        </button>
      </div>
    </div>
  )
}
