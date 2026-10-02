import { useEffect, useMemo, useRef, useState } from 'react'
import './calendar.css'

// Diverging scale: green = profit, red = loss, four steps per arm by size.
// Checked with the dataviz palette validator against the panel surface
// (#0e192d): each arm is monotone with visible step gaps, and its faintest step
// clears 2:1 (green 3.6, red 2.1). Same-size red and green would look alike to
// red-green colour-blind readers, so the green arm sits one lightness notch
// above the red: each profit step clears the CVD target (dE >= 8) against the
// loss step of the same size. The green is held to moderate chroma so a small
// profit does not shout louder than a small loss. Across sizes some pairs still
// collide under deuteranopia (a large profit against the largest loss), which
// is why every cell also prints its signed value: colour never carries profit
// or loss alone. Zero gets NO fill; break-even is an outline.
const PROFIT = ['#358122', '#4c983a', '#60ac4f', '#7dcb6c']
const LOSS = ['#763e49', '#984b52', '#b7565a', '#e66767']
// Ink per step, picked for >= 4.5:1 against that step's fill.
const PROFIT_INK = ['#ffffff', '#08101f', '#08101f', '#08101f']
const LOSS_INK = ['#ffffff', '#ffffff', '#ffffff', '#08101f']
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']

const rupees = v => `${v < 0 ? '−' : v > 0 ? '+' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
const compact = v => {
  const a = Math.abs(v), sign = v < 0 ? '−' : v > 0 ? '+' : ''
  return a >= 1000 ? `${sign}${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}k` : `${sign}${a.toFixed(0)}`
}
const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const monthName = (y, m) => new Date(y, m, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' })

// Size bins from the quartiles of |net| across the shown days, shared by both
// arms, so a step means the same rupee range on the profit and loss sides.
function binner(days) {
  const mags = days.map(d => Math.abs(d.pnl)).filter(v => v >= 0.5).sort((a, b) => a - b)
  const q = p => mags.length ? mags[Math.min(mags.length - 1, Math.floor(p * mags.length))] : 0
  const edges = [q(0.25), q(0.5), q(0.75)]
  return { edges, step: v => edges.filter(e => Math.abs(v) > e).length }
}

function cellStyle(day, step) {
  if (!day || Math.abs(day.pnl) < 0.5) return undefined
  const s = step(day.pnl)
  return day.pnl > 0
    ? { background: PROFIT[s], color: PROFIT_INK[s] }
    : { background: LOSS[s], color: LOSS_INK[s] }
}

function monthsOf(days) {
  const keys = [...new Set(days.map(d => d.date.slice(0, 7)))].sort().reverse()
  return keys.map(k => ({ year: +k.slice(0, 4), month: +k.slice(5, 7) - 1 }))
}

function weekdayCells(year, month) {
  // Mon..Fri grid: leading blanks up to the first weekday, weekends dropped.
  const first = new Date(year, month, 1)
  const cells = []
  const lead = (first.getDay() + 6) % 7          // Mon = 0
  if (lead < 5) for (let i = 0; i < lead; i++) cells.push(null)
  for (let d = new Date(first); d.getMonth() === month; d.setDate(d.getDate() + 1)) {
    const wd = (d.getDay() + 6) % 7
    if (wd < 5) cells.push(iso(d))
  }
  return cells
}

function Month({ year, month, byDate, span, step, selected, onPick, onHover }) {
  const cells = weekdayCells(year, month)
  const mine = cells.filter(Boolean).map(k => byDate[k]).filter(Boolean)
  const net = mine.reduce((s, d) => s + d.pnl, 0)
  const up = mine.filter(d => d.pnl > 0).length, down = mine.filter(d => d.pnl < 0).length
  return <section className="cal-month">
    <header><h4>{monthName(year, month)}</h4><span>{rupees(net)} · {up} up · {down} down</span></header>
    <div className="cal-grid" role="grid" aria-label={monthName(year, month)}>
      {WEEKDAYS.map(w => <div key={w} className="cal-weekday" role="columnheader">{w}</div>)}
      {cells.map((k, i) => {
        if (!k) return <div key={`pad${i}`} className="cal-pad" aria-hidden="true"/>
        // Before the first or after the last session in this log: not a day
        // with no trades, just a day the log does not cover.
        if (k < span[0] || k > span[1]) return <div key={k} className="cal-out" aria-hidden="true">{+k.slice(8)}</div>
        const day = byDate[k]
        const label = day
          ? `${k}: ${rupees(day.pnl)} net, ${day.trades} trades, ${day.wins} winners, gross ${rupees(day.gross)}, costs ₹${day.cost.toFixed(0)}`
          : `${k}: no trades, or a market holiday`
        const cls = ['cal-cell', day ? (Math.abs(day.pnl) < 0.5 ? 'flat' : '') : 'empty', selected === k ? 'selected' : ''].join(' ')
        return <button key={k} type="button" className={cls} style={cellStyle(day, step)}
          aria-label={label} disabled={!day}
          onClick={() => day && onPick(k)} onMouseEnter={() => onHover(day ? k : null)}
          onFocus={() => onHover(day ? k : null)} onMouseLeave={() => onHover(null)}>
          <span className="cal-dom">{+k.slice(8)}</span>
          {day && <span className="cal-val">{Math.abs(day.pnl) < 0.5 ? '0' : compact(day.pnl)}</span>}
        </button>
      })}
    </div>
  </section>
}

function Legend({ edges }) {
  const [a, b, c] = edges.map(v => `₹${Math.round(v).toLocaleString('en-IN')}`)
  const ranges = [`up to ${a}`, `${a}–${b}`, `${b}–${c}`, `over ${c}`]
  return <div className="cal-legend" aria-label="Colour key">
    <span className="cal-legend-label">Loss</span>
    {[...LOSS].reverse().map((col, i) => <span key={col} className="cal-swatch" style={{ background: col }} title={`Loss ${ranges[3 - i]}`}/>)}
    <span className="cal-swatch flat" title="Break-even"/>
    {PROFIT.map((col, i) => <span key={col} className="cal-swatch" style={{ background: col }} title={`Profit ${ranges[i]}`}/>)}
    <span className="cal-legend-label">Profit</span>
    <span className="cal-swatch empty" title="No trades or market holiday"/><span className="cal-legend-label">no trades / holiday</span>
    <small>Darker to brighter = larger day: {ranges.join(' · ')}</small>
  </div>
}

// Most profitable first, the worst loss last. Ties keep the log's order.
const byProfit = trades => [...(trades || [])].sort((a, b) => (b.pnl ?? 0) - (a.pnl ?? 0))

function DayTrades({ date, trades }) {
  const ref = useRef(null)
  // The table sits below every month; bring it into view when a day is picked.
  useEffect(() => { ref.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, [date])
  if (!trades?.length) return null
  const rows = byProfit(trades)
  const net = rows.reduce((s, t) => s + (t.pnl ?? 0), 0)
  const wins = rows.filter(t => (t.pnl ?? 0) > 0).length
  return <div className="panel scroll cal-detail" ref={ref}>
    <h3>Trades on {date}</h3>
    <p className="cal-detail-sub">{rows.length} trades · {wins} winners · {rupees(net)} net · sorted most profitable first</p>
    <table><thead><tr><th>#</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>In</th><th>Out</th><th>Exit reason</th><th>Gross</th><th>Costs</th><th>Net</th></tr></thead>
      <tbody>{rows.map((t, i) => <tr key={i}>
        <td>{i + 1}</td><td>{t.symbol}</td><td>{t.side}</td><td>{t.qty}</td><td>{t.entry}</td><td>{t.exit}</td>
        <td>{t.entry_time}</td><td>{t.exit_time}</td><td>{t.reason}</td>
        <td>{rupees(t.gross ?? 0)}</td><td>₹{(t.cost ?? 0).toFixed(1)}</td>
        <td><b>{rupees(t.pnl ?? 0)}</b></td>
      </tr>)}</tbody></table>
  </div>
}

export default function Calendar({ api }) {
  const [data, setData] = useState(null), [error, setError] = useState('')
  const [log, setLog] = useState(''), [strategy, setStrategy] = useState('')
  const [selected, setSelected] = useState(null), [hover, setHover] = useState(null)
  const [asTable, setAsTable] = useState(false)

  const load = async (nextLog = log, nextStrategy = strategy) => {
    const qs = new URLSearchParams()
    if (nextLog) qs.set('log', nextLog)
    if (nextStrategy) qs.set('strategy', nextStrategy)
    try {
      const d = await api(`/api/calendar${qs.toString() ? `?${qs}` : ''}`)
      setData(d); setError(''); setSelected(null)
      setLog(d.log?.id || ''); setStrategy(d.strategy || '')
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load('', '') }, [])

  const days = data?.days || []
  const byDate = useMemo(() => Object.fromEntries(days.map(d => [d.date, d])), [days])
  const { edges, step } = useMemo(() => binner(days), [days])
  const span = days.length ? [days[0].date, days[days.length - 1].date] : ['', '']
  const s = data?.summary || {}
  const focus = byDate[hover] || byDate[selected]

  if (error) return <div className="empty-state">Could not load trade logs: {error}</div>
  if (!data) return <div className="empty-state">Loading trade logs…</div>
  if (!data.logs.length) return <div className="empty-state">No trade logs yet. Run <code>.\run-backtest.ps1 -Compare</code>, or start paper trading, and the days will appear here.</div>

  return <section className="workspace">
    <div className="section-heading">
      <div><h2>P&amp;L calendar</h2><span>Each square is one session: the net of every position opened that day, after costs.</span></div>
      <div className="actions">
        <select aria-label="Trade log" value={log} onChange={e => load(e.target.value, '')}>
          {data.logs.map(l => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
        <select aria-label="Strategy" value={strategy} onChange={e => load(log, e.target.value)}>
          {data.strategies.map(n => <option key={n} value={n}>{n}</option>)}
        </select>
        <button className="quiet" onClick={() => setAsTable(v => !v)}>{asTable ? 'Calendar view' : 'Table view'}</button>
        <button className="quiet" onClick={() => load()}>Reload</button>
      </div>
    </div>

    {s.sessions ? <div className="stats">
      <div className="metric"><span>Net P&amp;L</span><strong>{rupees(s.net)}</strong><small>gross {rupees(s.gross)} · costs ₹{Math.round(s.cost).toLocaleString('en-IN')}</small></div>
      <div className="metric"><span>Profitable days</span><strong>{s.profitable_days} / {s.sessions}</strong><small>{(s.profitable_days / s.sessions * 100).toFixed(0)}% · {s.losing_days} losing · {s.flat_days} flat</small></div>
      <div className="metric"><span>Best day</span><strong>{rupees(s.best.pnl)}</strong><small>{s.best.date}</small></div>
      <div className="metric"><span>Worst day</span><strong>{rupees(s.worst.pnl)}</strong><small>{s.worst.date} · longest losing run {s.longest_losing_streak} days</small></div>
    </div> : <div className="empty-state">This log has no trades for {strategy}.</div>}

    {asTable ? <div className="panel scroll">
      <table><thead><tr><th>Date</th><th>Trades</th><th>Winners</th><th>Gross</th><th>Costs</th><th>Net</th></tr></thead>
        <tbody>{[...days].reverse().map(d => <tr key={d.date} onClick={() => setSelected(d.date)} className="cal-row">
          <td>{d.date}</td><td>{d.trades}</td><td>{d.wins}</td><td>{rupees(d.gross)}</td><td>₹{d.cost.toFixed(0)}</td>
          <td><b>{rupees(d.pnl)}</b></td></tr>)}</tbody></table>
    </div> : <div className="panel">
      <Legend edges={edges}/>
      <output className="cal-readout" aria-live="polite">{focus
        ? <><b>{focus.date}</b> {rupees(focus.pnl)} net · {focus.trades} trades · {focus.wins} winners · gross {rupees(focus.gross)} · costs ₹{focus.cost.toFixed(0)}</>
        : 'Hover or focus a day for its numbers; click it for the trades.'}</output>
      <div className="cal-months">{monthsOf(days).map(({ year, month }) =>
        <Month key={`${year}-${month}`} year={year} month={month} byDate={byDate} span={span} step={step}
          selected={selected} onPick={setSelected} onHover={setHover}/>)}</div>
    </div>}

    {selected && <DayTrades date={selected} trades={data.trades[selected]}/>}
    <p className="note">A run of green days is not by itself evidence of an edge: a strategy with none still has winning streaks. Judge it on the session-clustered confidence intervals in the comparison report, not on this view.</p>
  </section>
}
