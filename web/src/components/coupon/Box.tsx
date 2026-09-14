/** A coupon box: a square you mark with a biro X. A real checkbox
 *  underneath, so keyboard and screen readers get the standard control;
 *  `labelledBy` is the id of the visible text that names it. */
export function Box({
  id,
  checked,
  onChange,
  labelledBy,
}: {
  id: string
  checked: boolean
  onChange: (checked: boolean) => void
  labelledBy: string
}) {
  return (
    <label className="box" htmlFor={id}>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        aria-labelledby={labelledBy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 3.5 L20.5 20" />
        <path d="M20 4 L3.5 20.5" />
      </svg>
    </label>
  )
}
