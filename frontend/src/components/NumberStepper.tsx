type Props = {
  value: string;
  onChange: (value: string) => void;
  /** Size of one step from the current value, going up or down. */
  step: (current: number, direction: 1 | -1) => number;
  min: number;
  decimals: number;
  placeholder: string;
  label: string;
};

/** A number field with up/down arrows; the arrow keys step the same way. */
function NumberStepper({ value, onChange, step, min, decimals, placeholder, label }: Props) {
  function nudge(direction: 1 | -1) {
    const parsed = Number(value);
    // An empty field starts from its first step (0.1, or the minimum).
    const next = value === '' || Number.isNaN(parsed)
      ? Math.max(min, step(0, 1))
      : Math.max(min, parsed + direction * step(parsed, direction));
    const factor = 10 ** decimals;
    onChange(String(Math.round(next * factor) / factor));
  }
  return <span className="stepper">
    <input value={value} onChange={event => onChange(event.target.value.replace(decimals ? /[^\d.]/g : /\D/g, ''))}
      onKeyDown={event => {
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); nudge(event.key === 'ArrowUp' ? 1 : -1); }
      }}
      inputMode={decimals ? 'decimal' : 'numeric'} placeholder={placeholder} aria-label={label}/>
    <span className="stepper-arrows">
      <button type="button" tabIndex={-1} onClick={() => nudge(1)} aria-label={`Increase ${label.toLowerCase()}`}><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m3 7.5 3-3 3 3"/></svg></button>
      <button type="button" tabIndex={-1} onClick={() => nudge(-1)} disabled={value === '' || Number(value) <= min} aria-label={`Decrease ${label.toLowerCase()}`}><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m3 4.5 3 3 3-3"/></svg></button>
    </span>
  </span>;
}

export default NumberStepper;
