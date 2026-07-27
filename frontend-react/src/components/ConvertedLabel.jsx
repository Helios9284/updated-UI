export function ConvertedLabel({ label, fallback, title = "" }) {
  if (label) {
    return (
      <span className="converted-label" title={title || label}>
        {label}
      </span>
    );
  }
  return fallback;
}
