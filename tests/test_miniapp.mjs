// tests/test_miniapp.mjs — офлайн-проверки логики Mini App.
//
// Скрипт страницы загружается в заглушённое окружение (без браузера и сети),
// после чего проверяются чистые функции: форматирование валюты, отсчёт до
// торгов, тяжесть повреждения, группировка по дням.
//
// Запуск:  node tests/test_miniapp.mjs

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import vm from 'node:vm'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const html = readFileSync(join(root, 'miniapp', 'index.html'), 'utf8')
const script = html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/)[1]

// Минимальные заглушки: скрипт при загрузке трогает DOM и localStorage
const noop = () => {}
const el = () => ({
  classList: {add: noop, remove: noop, toggle: noop, contains: () => false},
  style: {}, addEventListener: noop, textContent: '', innerHTML: '',
  querySelectorAll: () => [], remove: noop, children: [],
})
const documentStub = {
  getElementById: el, querySelector: el, querySelectorAll: () => [],
  body: {classList: {add: noop, remove: noop}},
  addEventListener: noop,
}
const sandbox = {
  document: documentStub,
  window: {location: {origin: 'https://example.com'}, Telegram: undefined},
  localStorage: {getItem: () => null, setItem: noop},
  fetch: () => Promise.reject(new Error('сеть в тестах не используется')),
  setTimeout: noop, setInterval: noop, clearTimeout: noop,
  requestAnimationFrame: noop, performance: {now: () => 0},
  navigator: {}, console,
}
sandbox.globalThis = sandbox
vm.createContext(sandbox)
// Стрелки, объявленные через const, не попадают в глобальный объект vm —
// вытаскиваем их явно
vm.runInContext(script + '\n;globalThis.__exp = {cpDamage, cpPrice, esc};', sandbox)

let failed = 0
// toLocaleString('ru-RU') разделяет разряды неразрывным пробелом:
// строки выглядят одинаково, но байты разные — приводим к обычному пробелу
const norm = v => typeof v === 'string' ? v.replace(/ | /g, ' ') : v
const eq = (actual, expected, name) => {
  const same = JSON.stringify(norm(actual)) === JSON.stringify(norm(expected))
  if (!same) { failed++; console.log(`  FAIL ${name}\n       ждали ${JSON.stringify(expected)}, получили ${JSON.stringify(actual)}`) }
  else console.log(`  OK   ${name}`)
}
const ok = (cond, name) => eq(!!cond, true, name)

const { fmtP, cpSeverity, cpAuctionLabel, cpDayLabel } = sandbox
const { cpDamage, cpPrice, esc } = sandbox.__exp

// ── Валюта: главный дефект, из-за которого доллары подписывались рублями ──────
eq(fmtP(8202, {source: 'copart', currency: 'USD'}), '$8 202', 'доллары Copart')
eq(fmtP(8202, {source: 'copart', currency: 'CAD'}), 'CA$8 202', 'канадские доллары')
eq(fmtP(500000, {source: 'avito'}), '500 000 ₽', 'рубли российских площадок')
eq(fmtP(0, {source: 'copart'}), '—', 'нулевая цена')
eq(fmtP(1000, null), '1 000 ₽', 'без объекта — рубли, как раньше')

// ── Тяжесть повреждения ──────────────────────────────────────────────────────
eq(cpSeverity('BURN'), 'severe', 'пожар — тяжёлое')
eq(cpSeverity('WATER/FLOOD'), 'severe', 'потоп — тяжёлое')
eq(cpSeverity('HAIL'), 'minor', 'град — лёгкое')
eq(cpSeverity('MINOR DENT/SCRATCHES'), 'minor', 'царапины — лёгкое')
eq(cpSeverity('FRONT END'), 'medium', 'перед — среднее')
eq(cpSeverity(null), 'medium', 'неизвестное — среднее')

// ── Отсчёт до торгов ─────────────────────────────────────────────────────────
const at = h => new Date(Date.now() + h * 3600000).toISOString().replace('T', ' ')
ok(cpAuctionLabel(at(0.4)).text.includes('мин'), 'меньше часа — минуты')
eq(cpAuctionLabel(at(0.4)).level, 'urgent', 'меньше часа — срочно')
eq(cpAuctionLabel(at(3)).level, 'urgent', '3 часа — срочно')
ok(cpAuctionLabel(at(20)).text.startsWith('сегодня'), '20 часов — сегодня')
ok(cpAuctionLabel(at(30)).text.startsWith('завтра'), '30 часов — завтра')
eq(cpAuctionLabel(at(30)).level, 'soon', 'завтра — скоро')
ok(cpAuctionLabel(at(96)).text.includes('через 4 дн'), '4 суток')
eq(cpAuctionLabel(at(480)).level, '', 'через 20 дней — без подсветки')
eq(cpAuctionLabel(at(-5)).text, 'торги прошли', 'прошедшие торги')
eq(cpAuctionLabel(null), null, 'без даты')

// ── Группировка по дням ──────────────────────────────────────────────────────
eq(cpDayLabel(at(2)), 'Сегодня', 'сегодняшние торги')
eq(cpDayLabel(null), 'Дата торгов не назначена', 'дата не назначена')

// ── Прочее ───────────────────────────────────────────────────────────────────
eq(cpDamage('FRONT END'), 'Перед', 'перевод повреждения')
eq(cpPrice(4500), '$4 500', 'цена лота в долларах')
eq(esc('<b>&"</b>'), '&lt;b&gt;&amp;&quot;&lt;/b&gt;', 'экранирование HTML')

console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВСЕ ТЕСТЫ ПРОШЛИ')
process.exit(failed ? 1 : 0)
