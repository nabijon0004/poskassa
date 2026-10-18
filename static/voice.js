// Voice announcements («Ваш заказ принят…», «Заказ номер N готов») in a calm, natural voice,
// like at an airport: a soft three-tone chime, then the phrase. Edge has Microsoft's neural
// "Online (Natural)" voices (they need internet); otherwise the best installed voice is used.
const PREFERRED_VOICES = {
  ru: ["Светлана Online (Natural)", "Svetlana Online (Natural)", "Дария", "Dariya", "Светлана", "Svetlana"],
  en: ["Jenny Online (Natural)", "Aria Online (Natural)", "Ava Online (Natural)", "Emma Online (Natural)"],
};
// Calm announcement pace; neural voices sound natural slightly slower than default.
const ANNOUNCEMENT_RATE = 0.9;
const CHIME_SECONDS = 1.6;

function speechVoices() {
  const voices = window.speechSynthesis.getVoices();
  if (voices.length) return Promise.resolve(voices);
  // Voices load asynchronously the first time.
  return new Promise((resolve) => {
    const done = () => resolve(window.speechSynthesis.getVoices());
    window.speechSynthesis.addEventListener("voiceschanged", done, { once: true });
    window.setTimeout(done, 1500);
  });
}

function pickVoice(voices, language) {
  const code = language.slice(0, 2).toLowerCase();
  const matching = voices.filter((voice) => voice.lang.toLowerCase().startsWith(code));
  for (const name of PREFERRED_VOICES[code] || []) {
    const voice = matching.find((entry) => entry.name.includes(name));
    if (voice) return voice;
  }
  return matching.find((voice) => /natural/i.test(voice.name)) || matching.find((voice) => !voice.localService) || matching[0] || null;
}

// A soft «ding-dong-ding» like an airport announcement chime.
function playChime() {
  try {
    const context = new AudioContext();
    [[659.25, 0], [523.25, 0.42], [783.99, 0.84]].forEach(([frequency, offset]) => {
      const start = context.currentTime + offset;
      const oscillator = context.createOscillator();
      const overtone = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = frequency;
      overtone.type = "sine";
      overtone.frequency.value = frequency * 2;
      const overtoneGain = context.createGain();
      overtoneGain.gain.value = 0.15;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.28, start + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 1.1);
      oscillator.connect(gain);
      overtone.connect(overtoneGain).connect(gain);
      gain.connect(context.destination);
      [oscillator, overtone].forEach((node) => { node.start(start); node.stop(start + 1.15); });
    });
    window.setTimeout(() => context.close(), 2600);
  } catch { /* Sound is optional. */ }
}

// Speaks `text` in `language` (BCP 47, e.g. "ru-RU"); `fallbackLanguage` is used when there
// is no voice for the language (Tajik is read by the Russian voice).
async function announce(text, language, { fallbackLanguage = "ru-RU", chime = true } = {}) {
  if (!("speechSynthesis" in window)) {
    if (chime) playChime();
    return;
  }
  const voices = await speechVoices();
  let voice = pickVoice(voices, language);
  let lang = language;
  if (!voice && fallbackLanguage) {
    voice = pickVoice(voices, fallbackLanguage);
    lang = fallbackLanguage;
  }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = voice?.lang || lang;
  if (voice) utterance.voice = voice;
  utterance.rate = ANNOUNCEMENT_RATE;
  utterance.pitch = 1;
  window.speechSynthesis.cancel();
  if (chime) playChime();
  window.setTimeout(() => window.speechSynthesis.speak(utterance), chime ? CHIME_SECONDS * 1000 : 0);
  return voice;
}
