import { useEffect, useMemo, useState } from 'react'

const emptyScan = { signals: [], errors: [], scan_date: null }
const emptyContext = { generated_at: null, markets: [], volatility: [], news: [], errors: [], regime: 'Unavailable', score: 0, summary: 'Refresh global context to load the overnight backdrop.' }
const emptyBudgetPlan = { date: null, budget: 10000, max_positions: 2, slot_budget: 0, deployed: 0, unused_cash: 10000, total_risk: 0, risk_pct: 0, total_reward: 0, rows: [], note: 'Refresh all strategies to build the intraday plan.' }
const money = value => `₹${Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
const number = value => Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })
const signed = value => `${Number(value) > 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`
const tickPrice = (value, tick = 0.05) => Number((Math.round(Number(value || 0) / tick) * tick).toFixed(4))

async function api(path, options) {
  let response
  try {
    response = await fetch(path, options)
  } catch {
    throw Error('Cannot reach the local API on 127.0.0.1:8788.')
  }
  // The dev proxy answers with non-JSON when nothing is listening on 8788, so
  // parse defensively rather than surfacing a JSON syntax error to the user.
  const text = await response.text()
  let body
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    throw Error(response.ok ? 'The local API returned a malformed response.' : `The local API is not reachable (HTTP ${response.status}).`)
  }
  // An empty body on a failed response means the dev proxy could not reach the
  // API at all, which is a different problem from the API rejecting the call.
  if (!response.ok) throw Error(body.error || (text ? `Request failed (HTTP ${response.status}).` : 'Cannot reach the local API on 127.0.0.1:8788. Is unified_api.py running?'))
  return body
}

function Scanner({ data, run, loading, disabled }) {
  const [bulls, bears] = useMemo(() => {
    const sorted = [...data.signals].sort((a, b) => (b.crossover_date || '').localeCompare(a.crossover_date || ''))
    return [sorted.filter(row => row.direction === 'Bullish'), sorted.filter(row => row.direction === 'Bearish')]
  }, [data])
  return <section className="workspace">
    <div className="section-heading"><div><p className="eyebrow">ALGO TRADING</p><h2>NIFTY 100 trend scan</h2><span>SMA 6 / SMA 30 with an illustrative daily setup.</span></div><button onClick={run} disabled={disabled}>{loading ? 'Scanning…' : 'Run NIFTY 100 scan'}</button></div>
    <div className="stats"><Metric label="Analysed" value={data.signals.length}/><Metric label="Bullish" value={bulls.length} tone="up"/><Metric label="Bearish" value={bears.length} tone="down"/></div>
    <SignalTable title="Bullish signals" rows={bulls} tone="up"/><SignalTable title="Bearish signals" rows={bears} tone="down"/>
    <p className="note">The scanner is research only. Its illustrative daily backtest excludes costs, taxes, slippage and survivorship bias.</p>
  </section>
}

function Metric({ label, value, tone = '' }) { return <article className={`metric ${tone}`}><span>{label}</span><strong>{value}</strong></article> }

function SignalTable({ title, rows, tone }) {
  return <section className="panel"><h3><i className={tone}/>{title}</h3><div className="scroll"><table><thead><tr><th>Symbol</th><th>Trend</th><th>Setup</th><th>Illustrative plan</th><th>Backtest*</th></tr></thead><tbody>{rows.length ? rows.map(row => <tr key={row.symbol}><td><b>{row.symbol}</b><small>{row.name}</small></td><td>{row.crossover_date || 'No cross in lookback'}<small>Close {money(row.close)} · spread {row.spread_pct >= 0 ? '+' : ''}{row.spread_pct?.toFixed(2)}%</small></td><td>{row.risk_note}<small>Volume {row.volume_ratio?.toFixed(2)}× · 20D range {row.range_position_pct?.toFixed(0)}%</small></td><td>Entry {money(row.entry)}<small>Stop {money(row.stop)} · target {money(row.target)}</small></td><td>{row.backtest_trades} trades<small>{row.backtest_win_rate?.toFixed(0)}% wins · {row.backtest_avg_return?.toFixed(2)}% avg</small></td></tr>) : <tr><td colSpan="5">No signals yet.</td></tr>}</tbody></table></div></section>
}

function Orb({ plan, backtest, runPlan, runBacktest, loading }) {
  const summary = backtest?.summary
  return <section className="workspace">
    <div className="section-heading"><div><p className="eyebrow">ORB TRADING</p><h2>Opening range breakout</h2><span>Shared Kite feed · read-only levels and historical simulation.</span></div><div className="actions"><button className="quiet" onClick={() => runPlan(true)} disabled={loading}>Pre-market list</button><button onClick={() => runPlan(false)} disabled={loading}>Build today’s plan</button><button className="quiet" onClick={runBacktest} disabled={loading}>Run backtest</button></div></div>
    {plan && <section className="panel"><h3>ORB plan · {plan.date}</h3><p className="watchlist"><b>Watchlist:</b> {plan.symbols.join(', ') || 'No eligible symbols'}</p>{plan.premarket ? <p>Pre-market list only. Build the plan after the opening range has closed.</p> : <PlanTable rows={plan.rows}/>} {plan.note && <p className="note">{plan.note}</p>}</section>}
    {summary && <><div className="stats"><Metric label="Trades" value={summary.trades}/><Metric label="Win rate" value={`${summary.win_rate || 0}%`} tone="up"/><Metric label="Net P&L" value={money(summary.net)} tone={summary.net >= 0 ? 'up' : 'down'}/><Metric label="Max drawdown" value={money(summary.max_drawdown)} tone="down"/></div><section className="panel"><h3>ORB backtest trades</h3><div className="scroll"><table><thead><tr><th>Date</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P&L</th></tr></thead><tbody>{backtest.trades.map((row, index) => <tr key={`${row.date}-${row.symbol}-${index}`}><td>{row.date}</td><td><b>{row.symbol}</b></td><td>{row.side}</td><td>{money(row.entry)}</td><td>{money(row.exit)}</td><td>{row.reason}</td><td className={row.pnl >= 0 ? 'positive' : 'negative'}>{money(row.pnl)}</td></tr>)}</tbody></table></div></section></>}
    {!plan && !summary && <section className="empty-state">Choose a pre-market list, build the opening-range plan after 10:00 IST, or run the historical backtest.</section>}
  </section>
}

function PlanTable({ rows }) { return <div className="scroll"><table><thead><tr><th>Symbol</th><th>Side</th><th>Trigger</th><th>Stop</th><th>Target</th><th>Qty</th><th>R:R</th></tr></thead><tbody>{rows.length ? rows.map((row, index) => <tr key={`${row.symbol}-${row.side}-${index}`}><td><b>{row.symbol}</b></td><td>{row.side}</td><td>{money(row.trigger)}</td><td>{money(row.stop)}</td><td>{money(row.target)}</td><td>{row.qty}</td><td>{row.rr}</td></tr>) : <tr><td colSpan="7">No levels available.</td></tr>}</tbody></table></div> }

function Momentum({ data }) {
  const bullish = data.rows.filter(row => row.direction === 'Bullish').length
  const bearish = data.rows.filter(row => row.direction === 'Bearish').length
  return <section className="workspace"><div className="section-heading"><div><p className="eyebrow">ARTICLE STRATEGY</p><h2>Price and volume momentum</h2><span>Five- and twenty-day persistence, SMA spread, RSI 14, volume confirmation, and ATR.</span></div></div>
    <div className="stats"><Metric label="Scored" value={data.rows.length}/><Metric label="Bullish" value={bullish} tone="up"/><Metric label="Bearish" value={bearish} tone="down"/><Metric label="Neutral" value={data.rows.length - bullish - bearish}/></div>
    <section className="panel"><h3>Momentum ranking</h3><div className="scroll"><table><thead><tr><th>Symbol</th><th>Signal</th><th>Score</th><th>5-day</th><th>20-day</th><th>RSI 14</th><th>Volume</th><th>ATR</th></tr></thead><tbody>{data.rows.length ? data.rows.map(row => <tr key={row.symbol}><td><b>{row.symbol}</b><small>{row.name}</small></td><td className={row.direction === 'Bullish' ? 'positive' : row.direction === 'Bearish' ? 'negative' : ''}>{row.direction}</td><td>{row.score}</td><td>{row.return_5d > 0 ? '+' : ''}{row.return_5d}%</td><td>{row.return_20d > 0 ? '+' : ''}{row.return_20d}%</td><td>{row.rsi14}</td><td>{row.volume_ratio}×</td><td>{row.atr_pct}%</td></tr>) : <tr><td colSpan="8">Use Refresh all strategies to calculate momentum.</td></tr>}</tbody></table></div></section>
  </section>
}

function Execution({ data }) {
  return <section className="workspace"><div className="section-heading"><div><p className="eyebrow">ARTICLE EXECUTION METHODS</p><h2>Entry and risk planner</h2><span>Limit and stop-limit entries with ATR stops, 2R targets, and trailing distances.</span></div></div>
    <section className="panel"><h3>Research execution plans</h3><div className="scroll"><table><thead><tr><th>Symbol</th><th>Bias</th><th>Order method</th><th>Trigger / limit</th><th>Stop / target</th><th>Trail</th><th>Qty</th><th>Large-order handling</th></tr></thead><tbody>{data.rows.length ? data.rows.map(row => <tr key={row.symbol}><td><b>{row.symbol}</b></td><td>{row.direction}</td><td>{row.order_type}<small>{row.reason}</small></td><td>{row.trigger ? money(row.trigger) : '—'}<small>Limit {row.limit ? money(row.limit) : '—'}</small></td><td>{row.stop ? money(row.stop) : '—'}<small>Target {row.target ? money(row.target) : '—'}</small></td><td>{row.trailing_distance ? money(row.trailing_distance) : '—'}</td><td>{row.qty}</td><td>{row.iceberg}<small>{row.not_held}</small></td></tr>) : <tr><td colSpan="8">Use Refresh all strategies to create execution plans.</td></tr>}</tbody></table></div><p className="note">Market orders are not recommended by default because fill price is uncertain. Iceberg is flagged only for genuinely large size. Market-not-held remains discretionary and is never automated.</p></section>
  </section>
}

function GlobalContext({ data, refresh, loading, disabled }) {
  const markets = data.markets || [], volatility = data.volatility || [], news = data.news || []
  const advancing = markets.filter(row => row.change_pct > 0.05).length
  const falling = markets.filter(row => row.change_pct < -0.05).length
  const regimeTone = data.regime === 'Risk-on' ? 'positive' : data.regime === 'Risk-off' ? 'negative' : ''
  return <section className="workspace global-context">
    <div className="section-heading"><div><p className="eyebrow">OVERNIGHT RISK MAP</p><h2>Global market context</h2><span>Latest US, European and Asian index moves, volatility gauges, and market-moving world news.</span></div><button onClick={refresh} disabled={disabled}>{loading ? 'Refreshing…' : 'Refresh global context'}</button></div>
    <div className="stats"><Metric label="Risk regime" value={data.regime || 'Unavailable'} tone={data.regime === 'Risk-on' ? 'up' : data.regime === 'Risk-off' ? 'down' : ''}/><Metric label="Context score" value={data.score > 0 ? `+${data.score}` : data.score ?? 0}/><Metric label="Advancing" value={`${advancing} / ${markets.length}`} tone="up"/><Metric label="Declining" value={falling} tone="down"/></div>
    <section className="panel regime-panel"><div><p className="eyebrow">DERIVED BACKDROP</p><h3 className={regimeTone}>{data.regime || 'Unavailable'}</h3><p>{data.summary}</p></div><small>{data.generated_at ? `Refreshed ${new Date(data.generated_at).toLocaleString('en-IN')}` : 'Not refreshed yet'}</small></section>
    <section className="panel"><h3>Overnight and current global performance</h3><div className="scroll"><table><thead><tr><th>Region</th><th>Index</th><th>Last</th><th>Move</th><th>Absolute move</th><th>Feed status</th></tr></thead><tbody>{markets.length ? markets.map(row => <tr key={row.ticker}><td>{row.region}</td><td><b>{row.name}</b><small>{row.symbol}</small></td><td>{number(row.close)}</td><td className={row.direction === 'Up' ? 'positive' : row.direction === 'Down' ? 'negative' : ''}><b>{signed(row.change_pct)}</b></td><td>{row.change_abs > 0 ? '+' : ''}{number(row.change_abs)}</td><td>{row.data_mode}<small>{row.source}</small></td></tr>) : <tr><td colSpan="6">Refresh global context to load world indices.</td></tr>}</tbody></table></div><p className="note">Moves are versus each index’s prior close. Some feeds are delayed; the status is shown per row.</p></section>
    <section className="vix-grid">{volatility.length ? volatility.map(item => <article className="panel vix-card" key={item.name}><p className="eyebrow">VOLATILITY</p><div><h3>{item.name}</h3><span className={`badge ${item.state === 'Stress' || item.state === 'Elevated' ? 'low' : item.state === 'Low' ? 'high' : ''}`}>{item.state}</span></div><strong>{number(item.level)}</strong><p className={item.change_pct > 0 ? 'negative' : item.change_pct < 0 ? 'positive' : ''}>{signed(item.change_pct)} · {item.change > 0 ? '+' : ''}{number(item.change)}</p><small>Close {item.asof} · {item.source}</small></article>) : <section className="panel empty-vix">Volatility data is not available yet.</section>}</section>
    <section className="panel"><h3>World news with possible market impact</h3><div className="news-list">{news.length ? news.map((item, index) => <article className="news-item" key={`${item.url}-${index}`}><div><span className={`impact ${item.impact === 'Risk-positive' ? 'positive-impact' : item.impact === 'Risk-negative' ? 'negative-impact' : ''}`}>{item.impact}</span><span className="category">{item.category}</span></div><a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a><small>{item.source} · {item.published ? new Date(item.published).toLocaleString('en-IN') : 'Recent'}</small></article>) : <p className="note">No matching headlines were returned.</p>}</div><p className="note">Headline impact uses visible keyword rules and is context only; open the source before drawing a conclusion.</p></section>
    {data.errors?.length > 0 && <section className="source-errors"><b>Partial refresh:</b> {data.errors.join(' · ')}</section>}
  </section>
}

function IntradayBudgetPlan({ data, calculate, loading }) {
  const [budget, setBudget] = useState(data.budget || 10000)
  const send = event => { event.preventDefault(); calculate(Number(budget)) }
  const rows = data.rows || []
  return <section className="workspace budget-workspace"><div className="section-heading"><div><p className="eyebrow">CASH-CAPPED INTRADAY PLAN</p><h2>₹{number(data.budget || 10000)} daily action plan</h2><span>Turn today’s ranked recommendations into whole-share position sizes without exceeding your cash budget.</span></div><form className="budget-form" onSubmit={send}><label>Daily intraday budget<div><span>₹</span><input type="number" min="500" max="10000000" step="500" value={budget} onChange={event => setBudget(event.target.value)} required/><button disabled={loading}>{loading ? 'Calculating…' : 'Build plan'}</button></div></label></form></div>
    <div className="stats"><Metric label="Daily budget" value={money(data.budget)}/><Metric label="Planned capital" value={money(data.deployed)} tone="up"/><Metric label="Unused cash" value={money(data.unused_cash)}/><Metric label="Stop risk" value={`${money(data.total_risk)} · ${data.risk_pct || 0}%`} tone="down"/></div>
    <section className="panel plan-brief"><div><p className="eyebrow">TODAY’S RULE</p><h3>{rows.length ? `${rows.length} planned position${rows.length === 1 ? '' : 's'} · up to ${money(data.slot_budget)} each` : 'No capital allocated'}</h3><p>{data.note}</p></div><div><span>Potential gross reward</span><strong>{money(data.total_reward)}</strong></div></section>
    <section className="panel"><h3>Proposed sequence</h3><div className="scroll"><table><thead><tr><th>Priority</th><th>Symbol</th><th>Action</th><th>Latest</th><th>Entry condition</th><th>Qty</th><th>Capital</th><th>Stop</th><th>Target</th><th>Risk / reward</th><th>Confidence</th></tr></thead><tbody>{rows.length ? rows.map(row => <tr key={row.symbol}><td>{row.rank}</td><td><b>{row.symbol}</b><small>{row.name}</small></td><td className={row.side === 'LONG' ? 'positive' : 'negative'}><b>{row.side}</b><small>{row.order_type}</small></td><td>{row.last_price ? money(row.last_price) : 'Unavailable'}</td><td>{money(row.entry)}<small>{row.action}</small></td><td><b>{row.quantity}</b></td><td>{money(row.deployed)}<small>Slot {money(row.slot_budget)}</small></td><td className="negative">{row.stop ? money(row.stop) : '—'}<small>{money(row.risk)} planned risk</small></td><td className="positive">{row.target ? money(row.target) : '—'}<small>{money(row.reward)} gross reward</small></td><td>{row.rr ? `${row.rr}:1` : '—'}</td><td><span className={`badge ${(row.confidence || 'low').toLowerCase()}`}>{row.confidence || 'Low'}</span></td></tr>) : <tr><td colSpan="11">{data.note}</td></tr>}</tbody></table></div></section>
    <section className="panel checklist"><h3>Execution checklist</h3><ol><li>Wait for the stated entry condition; do not chase a price that has already moved away.</li><li>If one entry fills, reserve its allocated cash and do not reuse it for another row.</li><li>The stop and target are planning levels. Confirm protective exits separately in Kite.</li><li>Stop when the ₹{number(data.budget)} cash allocation or planned stop-risk is reached.</li></ol><p className="note">No leverage is assumed. Fees, taxes, slippage and gap risk are not included in the displayed stop-risk or reward.</p></section>
  </section>
}

function OrderTicket({ row, submit, close }) {
  const tick = Number(row.tick_size || 0.05), side = row.bias === 'LONG' ? 'BUY' : 'SELL'
  const stopLimit = String(row.plan.order_type || '').includes('STOP-LIMIT')
  const initialTrigger = tickPrice(row.plan.trigger || row.last_price, tick)
  const suggestedLimit = tickPrice(row.plan.limit || initialTrigger, tick)
  const initialLimit = stopLimit ? (side === 'BUY' ? Math.max(suggestedLimit, initialTrigger) : Math.min(suggestedLimit, initialTrigger)) : suggestedLimit
  const [quantity, setQuantity] = useState(row.plan.qty || 1), [product, setProduct] = useState('MIS'), [orderType, setOrderType] = useState(stopLimit ? 'SL' : 'LIMIT'), [price, setPrice] = useState(initialLimit), [trigger, setTrigger] = useState(initialTrigger), [confirmed, setConfirmed] = useState(false), [placing, setPlacing] = useState(false), [error, setError] = useState(''), [result, setResult] = useState(null)
  const send = async event => { event.preventDefault(); setPlacing(true); setError(''); try { setResult(await submit({ symbol: row.symbol, side, quantity: Number(quantity), product, order_type: orderType, price: Number(price), trigger_price: orderType === 'SL' ? Number(trigger) : null, confirmed })) } catch (problem) { setError(problem.message) } finally { setPlacing(false) } }
  const notional = Number(quantity || 0) * Number(price || 0)
  return <div className="modal-backdrop" role="presentation" onMouseDown={event => event.target === event.currentTarget && close()}><section className="order-ticket" role="dialog" aria-modal="true" aria-labelledby="ticket-title"><div className="ticket-heading"><div><p className="eyebrow">LIVE KITE ORDER</p><h2 id="ticket-title">Review {side} · {row.symbol}</h2><span>Recommendation: {row.bias} · latest Kite price {row.last_price ? money(row.last_price) : 'unavailable'}</span></div><button className="icon-button" type="button" onClick={close} aria-label="Close order ticket">×</button></div>
    {result ? <div className="order-success"><strong>Order submitted to Kite OMS</strong><p>Order ID: <code>{result.order_id}</code></p><small>{result.message}</small><button type="button" onClick={close}>Close</button></div> : <form onSubmit={send}><div className="ticket-grid"><label>Side<input value={side} disabled/></label><label>Quantity<input type="number" min="1" max="100000" step="1" value={quantity} onChange={event => setQuantity(event.target.value)} required/></label><label>Product<select value={product} onChange={event => setProduct(event.target.value)}><option value="MIS">MIS · intraday</option>{side === 'BUY' && <option value="CNC">CNC · delivery</option>}</select></label><label>Order type<select value={orderType} onChange={event => setOrderType(event.target.value)}><option value="LIMIT">Limit</option><option value="SL">Stop-limit</option></select></label><label>Limit price<input type="number" min={tick} step={tick} value={price} onChange={event => setPrice(event.target.value)} required/></label>{orderType === 'SL' && <label>Trigger price<input type="number" min={tick} step={tick} value={trigger} onChange={event => setTrigger(event.target.value)} required/></label>}</div><div className="ticket-summary"><span>Estimated value</span><strong>{money(notional)}</strong><small>DAY validity · NSE · tick size {tick}</small></div><label className="live-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)}/><span>I reviewed the symbol, side, quantity and prices. Submit this as a <b>live order</b>.</span></label>{error && <p className="ticket-error">{error}</p>}<div className="ticket-actions"><button className="quiet" type="button" onClick={close}>Cancel</button><button className="danger" disabled={!confirmed || placing}>{placing ? 'Submitting…' : `Place live ${side} order`}</button></div><p className="note">Kite returning an order ID means OMS submission only. It does not mean the exchange accepted or filled the order; verify it in the Kite order book.</p></form>}
  </section></div>
}

function Recommendations({ data, connected, refreshPrices, priceLoading, submitOrder }) {
  const rows = data.recommendations || []
  const actionable = rows.filter(row => row.bias !== 'WAIT')
  const high = rows.filter(row => row.confidence === 'High').length
  const [ticket, setTicket] = useState(null)
  return <section className="workspace"><div className="section-heading"><div><p className="eyebrow">DAILY CONSENSUS · FINAL TAB</p><h2>Unified recommendations</h2><span>SMA trend + momentum direction + ORB selection, with global risk context adjusting confidence only.</span></div><button className="quiet" onClick={refreshPrices} disabled={!connected || priceLoading || !rows.length}>{priceLoading ? 'Refreshing prices…' : 'Refresh latest prices'}</button></div>
    <div className="stats"><Metric label="Date" value={data.date || '—'}/><Metric label="Global backdrop" value={data.market_regime || 'Unavailable'} tone={data.market_regime === 'Risk-on' ? 'up' : data.market_regime === 'Risk-off' ? 'down' : ''}/><Metric label="Directional" value={actionable.length}/><Metric label="High confidence" value={high} tone="up"/></div>
    <section className="panel"><div className="panel-title"><h3>Cross-strategy ranking</h3><small>{data.prices_asof ? `Kite LTP refreshed ${new Date(data.prices_asof).toLocaleString('en-IN')}` : 'Latest prices not refreshed'}</small></div><div className="scroll"><table><thead><tr><th>Rank</th><th>Symbol</th><th>Latest price</th><th>Recommendation</th><th>Confidence</th><th>SMA</th><th>Momentum</th><th>ORB</th><th>Global</th><th>Entry plan</th><th>Risk levels</th><th>Order</th></tr></thead><tbody>{rows.length ? rows.map((row, index) => <tr key={row.symbol}><td>{index + 1}</td><td><b>{row.symbol}</b><small>{row.name}</small></td><td><b>{row.last_price == null ? 'Unavailable' : money(row.last_price)}</b><small>Kite LTP</small></td><td className={row.bias === 'LONG' ? 'positive' : row.bias === 'SHORT' ? 'negative' : ''}><b>{row.bias}</b><small>{row.action}</small></td><td><span className={`badge ${row.confidence.toLowerCase()}`}>{row.confidence}</span></td><td>{row.sma}</td><td>{row.momentum}<small>Score {row.momentum_score}</small></td><td>{row.orb_selected ? 'Selected' : 'No'}</td><td>{row.global_regime}<small>{row.global_note}</small></td><td>{row.plan.order_type}<small>{row.plan.trigger ? `Trigger ${money(row.plan.trigger)} · qty ${row.plan.qty}` : row.plan.reason}</small></td><td>{row.plan.stop ? `Stop ${money(row.plan.stop)}` : '—'}<small>{row.plan.target ? `Target ${money(row.plan.target)}` : ''}</small></td><td><button className="order-button" onClick={() => setTicket(row)} disabled={!connected || row.bias === 'WAIT' || row.last_price == null}>{row.bias === 'WAIT' ? 'No order' : 'Place order'}</button></td></tr>) : <tr><td colSpan="12">Connect Kite and use Refresh all strategies to generate today’s consensus.</td></tr>}</tbody></table></div><p className="note">A recommendation is conditional research, not a forecast. The order button opens a review ticket and never submits on the first click. Global context cannot create, reverse, or veto a signal.</p></section>
    {ticket && <OrderTicket key={ticket.symbol} row={ticket} submit={submitOrder} close={() => setTicket(null)}/>} 
  </section>
}

export default function App() {
  const [config, setConfig] = useState(null), [token, setToken] = useState(''), [scan, setScan] = useState(emptyScan), [momentum, setMomentum] = useState({ rows: [], scan_date: null }), [execution, setExecution] = useState({ rows: [], scan_date: null }), [marketContext, setMarketContext] = useState(emptyContext), [budgetPlan, setBudgetPlan] = useState(emptyBudgetPlan), [recommendations, setRecommendations] = useState({ date: null, market_regime: 'Unavailable', recommendations: [] }), [plan, setPlan] = useState(null), [backtest, setBacktest] = useState(null), [tab, setTab] = useState('scanner'), [message, setMessage] = useState(''), [loading, setLoading] = useState(false), [configError, setConfigError] = useState(null), [retrying, setRetrying] = useState(false)
  const applyDashboard = dashboard => { setScan(dashboard.scan || emptyScan); setMomentum(dashboard.momentum || { rows: [] }); setExecution(dashboard.execution || { rows: [] }); setPlan(dashboard.orb_plan || null); setMarketContext(dashboard.market_context || emptyContext); setBudgetPlan(dashboard.budget_plan || emptyBudgetPlan); setRecommendations(dashboard.recommendations || { date: null, market_regime: 'Unavailable', recommendations: [] }) }
  // Settled independently: a failing /api/dashboard must not take the Kite
  // login URL down with it.
  const load = async () => {
    const [settings, dashboard] = await Promise.allSettled([api('/api/config'), api('/api/dashboard')])
    if (settings.status === 'fulfilled') { setConfig(settings.value); setConfigError(null) } else setConfigError(settings.reason?.message || 'Request failed')
    if (dashboard.status === 'fulfilled') applyDashboard(dashboard.value)
    const failure = settings.reason || dashboard.reason
    if (failure) setMessage(`Start the local API: ${failure.message}`)
    return settings.status === 'fulfilled'
  }
  const retry = () => { setRetrying(true); load().finally(() => setRetrying(false)) }
  // The launcher opens this page right after spawning the API, so the first
  // /api/config can land before the server is listening. Back off and retry
  // rather than leaving the session panel dead until a manual reload.
  useEffect(() => {
    let cancelled = false, timer
    const attempt = async (delay, left) => {
      if (cancelled) return
      if (await load() || cancelled || left <= 0) return
      timer = setTimeout(() => attempt(Math.min(delay * 2, 5000), left - 1), delay)
    }
    attempt(500, 12)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [])
  const work = async (label, fn) => { setLoading(true); setMessage(label); try { await fn(); setMessage('Done.') } catch (error) { setMessage(error.message) } finally { setLoading(false) } }
  const connect = event => { event.preventDefault(); work('Connecting your local Kite session…', async () => { const result = await api('/api/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refreshToken: token }) }); setConfig(current => ({ ...current, connected: true, profile: result.profile })); setToken('') }) }
  const runScan = () => work('Scanning NIFTY 100 — about 40 seconds…', async () => { await api('/api/scan', { method: 'POST' }); applyDashboard(await api('/api/dashboard')) })
  const refreshAll = () => work('Refreshing strategies, overnight markets, VIX, news, and daily consensus — about a minute…', async () => { applyDashboard(await api('/api/refresh-all', { method: 'POST' })); setTab('recommendations') })
  const refreshContext = () => work('Refreshing global indices, VIX, and market-moving news…', async () => { applyDashboard(await api('/api/global-context', { method: 'POST' })) })
  const refreshPrices = () => work('Refreshing the latest Kite prices…', async () => { await api('/api/recommendation-prices', { method: 'POST' }); applyDashboard(await api('/api/dashboard')) })
  const calculateBudget = budget => work('Recalculating the cash-capped intraday plan…', async () => setBudgetPlan(await api('/api/intraday-plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ budget }) })))
  const submitOrder = payload => api('/api/orders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  const runPlan = premarket => work(premarket ? 'Fetching the pre-market watchlist…' : 'Fetching Kite candles and building the ORB plan…', async () => setPlan(await api('/api/orb/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ premarket }) })))
  const runBacktest = () => work('Running the ORB historical backtest — this can take a minute…', async () => setBacktest(await api('/api/orb/backtest', { method: 'POST' })))
  let content = <Scanner data={scan} run={runScan} loading={loading} disabled={loading || !config?.connected}/>
  if (tab === 'orb') content = <Orb plan={plan} backtest={backtest} runPlan={runPlan} runBacktest={runBacktest} loading={loading || !config?.connected}/>
  if (tab === 'momentum') content = <Momentum data={momentum}/>
  if (tab === 'execution') content = <Execution data={execution}/>
  if (tab === 'global') content = <GlobalContext data={marketContext} refresh={refreshContext} loading={loading} disabled={loading || !config?.connected}/>
  if (tab === 'budget') content = <IntradayBudgetPlan data={budgetPlan} calculate={calculateBudget} loading={loading}/>
  if (tab === 'recommendations') content = <Recommendations data={recommendations} connected={config?.connected} refreshPrices={refreshPrices} priceLoading={loading} submitOrder={submitOrder}/>
  return <main><header><div><p className="eyebrow">KITE CONNECT · LOCAL RESEARCH DESK</p><h1>One feed. One daily decision.</h1><span>SMA trend, momentum, ORB, global context, a cash-capped intraday plan, and one daily consensus.</span></div><button className="refresh-all" onClick={refreshAll} disabled={loading || !config?.connected}>{loading ? 'Refreshing…' : 'Refresh all strategies'}</button></header>
    <form className="auth" onSubmit={connect}><div><p className="eyebrow">SESSION</p><h2>{config?.connected ? `Connected${config.profile?.user_name ? ` as ${config.profile.user_name}` : ''}` : 'Connect Kite'}</h2></div><div className="login">{config?.login_url ? <a href={config.login_url} target="_blank" rel="noreferrer">Open Kite login ↗</a> : <span className="login-disabled" aria-disabled="true">Open Kite login ↗</span>}{config?.login_url ? <small>Sign in, then copy the one-time <code>request_token</code> from the redirect URL.</small> : <><small className="login-error">{configError ? `Login URL unavailable. ${configError}` : 'Waiting for the local API on 127.0.0.1:8788…'}</small><button type="button" className="quiet" onClick={retry} disabled={retrying}>{retrying ? 'Retrying…' : 'Retry'}</button></>}</div><label>Kite refresh token <small>(Kite calls this a request token)</small></label><div className="token-row"><input value={token} onChange={event => setToken(event.target.value)} placeholder="Paste the fresh request_token" required/><button disabled={loading}>{config?.connected ? 'Renew session' : 'Connect'}</button></div><small>Sent only to the local API. The session remains in memory. Live orders require a separate reviewed and confirmed ticket.</small></form>
    {message && <output>{message}</output>}<nav><button className={tab === 'scanner' ? 'selected' : ''} onClick={() => setTab('scanner')}>NIFTY 100 scanner</button><button className={tab === 'orb' ? 'selected' : ''} onClick={() => setTab('orb')}>ORB workspace</button><button className={tab === 'momentum' ? 'selected' : ''} onClick={() => setTab('momentum')}>Momentum</button><button className={tab === 'execution' ? 'selected' : ''} onClick={() => setTab('execution')}>Execution & risk</button><button className={tab === 'global' ? 'selected' : ''} onClick={() => setTab('global')}>Global context</button><button className={tab === 'budget' ? 'selected' : ''} onClick={() => setTab('budget')}>Intraday budget plan</button><button className={tab === 'recommendations' ? 'selected' : ''} onClick={() => setTab('recommendations')}>Daily recommendations</button></nav>{content}</main>
}
